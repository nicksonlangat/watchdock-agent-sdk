#!/usr/bin/env python3
"""
Nginx log collector.
Tails nginx access and error log files, parses lines using the watchdock log format,
and sends raw per-request access events and error events to WatchDock.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests


# Watchdock access log format:
# $remote_addr [$time_local] "$request" $status $body_bytes_sent $request_time $upstream_response_time $request_id
# Example: 203.0.113.1 [26/Feb/2026:10:23:01 +0000] "GET /api/ HTTP/1.1" 200 1234 0.043 0.041 5f8a9c3e1b2d4f6a8c9e0d1f2a3b4c5d
#
# $request_id is trailing and optional for backward compatibility: sources still using the
# older format (without it) keep working, they just won't get error/request correlation.
ACCESS_LOG_RE = re.compile(
    r'^(\S+) \[([^\]]+)\] "(\S+) (\S+)[^"]*" (\d+) (\d+) ([\d.]+|-) ([\d.]+|-)(?:\s+(\S+))?'
)

# Nginx error log format:
# 2026/02/26 10:23:01 [error] 12345#0: *1 message
# 2026/02/26 10:23:01 [warn]  12345#0: message (no asterisk prefix)
ERROR_LOG_RE = re.compile(
    r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] \d+#\d+: (?:\*\d+ )?(.+)$'
)

# Cap on events per POST so a large backlog (e.g. after a restart with no persisted
# state, or recovering from an extended backend outage) can't produce a payload big
# enough to get rejected with 413, which would otherwise drop the whole batch.
MAX_EVENTS_PER_BATCH = 200


class NginxLogCollector:
    """
    Reads nginx access and error log files for each configured NginxLogSource,
    parses new lines since the last collection, and sends raw per-request events
    and error events to the WatchDock backend.

    File read offsets are persisted to disk and only advanced after a
    successful send, so a process restart or a failed/oversized send never
    silently drops log lines. When a path is shared by multiple sources
    (e.g. filtered by different path prefixes), the offset for that path only
    advances once every source that reads it has sent successfully — a
    partial failure just means some events get retried next cycle rather
    than lost.
    """

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._state_path = self._resolve_state_path()
        # Byte offsets keyed by file path, persisted across restarts.
        self._file_positions: Dict[str, int] = self._load_positions()

    def _resolve_state_path(self) -> str:
        config_file = getattr(self.config, "config_file", "agent_config.json")
        directory = os.path.dirname(os.path.abspath(config_file))
        return os.path.join(directory, ".nginx_positions.json")

    def _load_positions(self) -> Dict[str, int]:
        try:
            with open(self._state_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.logger.warning(f"Could not load persisted nginx file positions, starting fresh: {e}")
            return {}

    def _save_positions(self) -> None:
        try:
            with open(self._state_path, "w") as f:
                json.dump(self._file_positions, f)
        except Exception as e:
            self.logger.warning(f"Could not persist nginx file positions: {e}")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def collect_and_send(self) -> None:
        """
        Called once per nginx_interval. For every configured NginxLogSource,
        reads new lines from its access and error log files, parses them,
        and sends to the backend. A file's read offset only advances once
        every source referencing it has sent its events successfully.
        """
        nginx_sources = self.config.get("nginx_sources", [])
        if not nginx_sources:
            return

        # Read each unique file path exactly once per cycle. Reading does not
        # mutate self._file_positions yet — that only happens once we know
        # every consumer of a path succeeded.
        access_lines_by_path: Dict[str, Tuple[List[str], int]] = {}
        error_lines_by_path: Dict[str, Tuple[List[str], int]] = {}
        path_succeeded: Dict[str, bool] = {}

        for source in nginx_sources:
            for store, path in [
                (access_lines_by_path, source["access_log_path"]),
                (error_lines_by_path, source["error_log_path"]),
            ]:
                if path not in store:
                    store[path] = self._read_new_lines(path)
                    path_succeeded[path] = True

        # Process each source with its collected lines.
        for source in nginx_sources:
            source_id = source["id"]
            prefix = source.get("filter_path_prefix", "")

            access_path = source["access_log_path"]
            access_lines, _ = access_lines_by_path.get(access_path, ([], 0))
            events = self._parse_access_lines(access_lines, prefix)
            if events and not self._send_access_events(source_id, events):
                path_succeeded[access_path] = False

            error_path = source["error_log_path"]
            error_lines, _ = error_lines_by_path.get(error_path, ([], 0))
            error_events = self._parse_error_lines(error_lines)
            if error_events and not self._send_error_events(source_id, error_events):
                path_succeeded[error_path] = False

        # Commit offsets only for paths where every source that reads them
        # sent successfully this cycle. Anything else is retried next cycle,
        # which may re-send already-delivered events for other sources of a
        # shared path — duplicates are an acceptable tradeoff for never
        # silently losing data.
        changed = False
        for path, (_, new_position) in {**access_lines_by_path, **error_lines_by_path}.items():
            if path_succeeded.get(path, True) and self._file_positions.get(path) != new_position:
                self._file_positions[path] = new_position
                changed = True

        if changed:
            self._save_positions()

    # ------------------------------------------------------------------
    # File reading
    # ------------------------------------------------------------------

    def _read_new_lines(self, path: str) -> Tuple[List[str], int]:
        """
        Read new lines from a log file since the last recorded offset.
        Handles log rotation by resetting to offset 0 when the file shrinks.
        Returns (lines, candidate_new_offset) without committing the offset —
        callers commit it only after a successful send. Returns ([], last
        known offset) if the file does not exist or cannot be read, so a
        transient read error never advances the persisted position.
        """
        last_position = self._file_positions.get(path, 0)

        if not os.path.exists(path):
            return [], last_position

        try:
            current_size = os.path.getsize(path)

            # Log rotation: file is smaller than our last position.
            if current_size < last_position:
                self.logger.info(f"Nginx log rotation detected: {path}")
                last_position = 0

            # No new content.
            if current_size <= last_position:
                return [], last_position

            lines: List[str] = []
            with open(path, "r", errors="replace") as f:
                f.seek(last_position)
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)
                new_position = f.tell()

            return lines, new_position

        except Exception as e:
            self.logger.error(f"Error reading nginx log file {path}: {e}")
            return [], last_position

    # ------------------------------------------------------------------
    # Access log parsing
    # ------------------------------------------------------------------

    def _parse_access_line(self, line: str) -> Optional[dict]:
        """
        Parse a single watchdock-format access log line into a raw event dict.
        Returns None if the line doesn't match.
        """
        match = ACCESS_LOG_RE.match(line)
        if not match:
            return None

        ip_address, time_local, method, raw_path, status_str, bytes_str, rt_str, _, request_id = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4),
            match.group(5),
            match.group(6),
            match.group(7),
            match.group(8),
            match.group(9),
        )

        # Parse timestamp. Format: 26/Feb/2026:10:23:01 +0000
        try:
            ts = datetime.strptime(time_local, "%d/%b/%Y:%H:%M:%S %z")
        except ValueError:
            return None

        # Strip query string from path.
        endpoint = urlparse(raw_path).path

        # Parse numeric fields.
        status_code = int(status_str)
        bytes_sent = int(bytes_str)
        response_ms = round(float(rt_str) * 1000, 2) if rt_str != "-" else None

        event = {
            "timestamp": ts.isoformat(),
            "ip_address": ip_address,
            "method": method.upper(),
            "endpoint": endpoint,
            "status_code": status_code,
            "response_ms": response_ms,
            "bytes_sent": bytes_sent,
        }
        if request_id:
            event["trace_id"] = request_id
        return event

    def _parse_access_lines(self, lines: List[str], prefix: str) -> List[dict]:
        """
        Parse access log lines into raw per-request event dicts.
        Applies prefix filter (agent side) if configured.
        """
        events: List[dict] = []
        for line in lines:
            parsed = self._parse_access_line(line)
            if parsed is None:
                continue
            if prefix and not parsed["endpoint"].startswith(prefix):
                continue
            events.append(parsed)
        return events

    # ------------------------------------------------------------------
    # Error log parsing
    # ------------------------------------------------------------------

    def _parse_error_lines(self, lines: List[str]) -> List[dict]:
        """
        Parse nginx error log lines into discrete event dicts.
        Skips lines that do not match the expected format.
        """
        events: List[dict] = []
        for line in lines:
            match = ERROR_LOG_RE.match(line)
            if not match:
                continue
            ts_str, level, message = match.group(1), match.group(2), match.group(3)
            try:
                ts = datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            events.append({
                "timestamp": ts.isoformat(),
                "level": level.lower(),
                "message": message.strip(),
            })
        return events

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _send_access_events(self, source_id: str, events: List[dict]) -> bool:
        """POST raw per-request access events to the backend, chunked to stay
        under any reasonable payload size limit. Returns True only if every
        chunk was accepted."""
        all_ok = True
        for chunk in self._chunk(events):
            if not self._post_events("nginx-access-events", source_id, chunk):
                all_ok = False
        return all_ok

    def _send_error_events(self, source_id: str, events: List[dict]) -> bool:
        """POST parsed error events to the backend, chunked the same way as
        access events. Returns True only if every chunk was accepted."""
        all_ok = True
        for chunk in self._chunk(events):
            if not self._post_events("nginx-error-events", source_id, chunk):
                all_ok = False
        return all_ok

    @staticmethod
    def _chunk(events: List[dict]) -> List[List[dict]]:
        return [
            events[i : i + MAX_EVENTS_PER_BATCH]
            for i in range(0, len(events), MAX_EVENTS_PER_BATCH)
        ] or [[]]

    def _post_events(self, endpoint: str, source_id: str, events: List[dict]) -> bool:
        if not events:
            return True

        try:
            response = requests.post(
                f"{self.config.get('api_endpoint')}/core/agent/{endpoint}/",
                json={"nginx_log_source_id": source_id, "events": events},
                headers={
                    "Authorization": f"Bearer {self.config.get('api_token')}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if response.status_code in (200, 201):
                self.logger.debug(
                    f"Sent {len(events)} events to {endpoint} for source {source_id}"
                )
                return True

            self.logger.warning(
                f"Failed to send {endpoint}: {response.status_code} - {response.text[:500]}"
            )
            return False
        except Exception as e:
            self.logger.error(f"Error sending {endpoint}: {e}")
            return False
