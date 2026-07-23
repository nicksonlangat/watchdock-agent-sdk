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
import time
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

# A source that's over its plan's monthly log limit gets HTTP 429 on every
# send no matter how often we retry. Without backing off, a stalled source
# still gets its (ever-growing) unsent backlog re-read and re-parsed every
# single cycle for nothing — the actual cost that motivated this backoff.
# Starts short so a transient/borderline limit recovers quickly, doubles on
# each further 429 to a 1-hour ceiling either because the calendar month
# rolled over or because the plan limit was raised.
QUOTA_BACKOFF_INITIAL_SECONDS = 300
QUOTA_BACKOFF_MAX_SECONDS = 3600


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

    A source whose organization is over its plan's monthly nginx-log limit
    gets backed off (see QUOTA_BACKOFF_*) instead of being retried every
    cycle: its send will fail regardless, so retrying immediately only buys
    another full read-and-parse of its unsent backlog for nothing. Backoff
    state is in-memory only (reset on restart) and keyed by source id, not
    path — see collect_and_send for how that interacts with path sharing.
    """

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._state_path = self._resolve_state_path()
        # Byte offsets keyed by file path, persisted across restarts.
        self._file_positions: Dict[str, int] = self._load_positions()
        # source_id -> (monotonic time backoff ends, current backoff duration).
        # The duration is kept alongside the deadline so a repeat 429 after
        # this backoff expires can double it rather than restarting at the
        # initial value.
        self._quota_backoff: Dict[str, Tuple[float, float]] = {}

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
    # Quota backoff
    # ------------------------------------------------------------------

    def _is_backed_off(self, source_id: str) -> bool:
        entry = self._quota_backoff.get(source_id)
        return entry is not None and time.monotonic() < entry[0]

    def _register_quota_exceeded(self, source_id: str) -> None:
        """Start or extend backoff for a source that just got HTTP 429.

        A no-op if it's already backing off: a large unsent backlog can
        span many chunks, and every chunk in the same cycle will also come
        back 429 — without this guard each of those would double the
        duration in turn, so one over-limit cycle could jump straight to
        the 1-hour ceiling instead of the intended gradual ramp.
        """
        if self._is_backed_off(source_id):
            return
        _, prev_duration = self._quota_backoff.get(source_id, (0.0, 0.0))
        duration = (
            min(prev_duration * 2, QUOTA_BACKOFF_MAX_SECONDS)
            if prev_duration
            else QUOTA_BACKOFF_INITIAL_SECONDS
        )
        self._quota_backoff[source_id] = (time.monotonic() + duration, duration)
        self.logger.info(
            f"Nginx source {source_id} is over its plan's monthly log limit; "
            f"backing off for {int(duration)}s instead of retrying every cycle"
        )

    def _clear_backoff(self, source_id: str) -> None:
        self._quota_backoff.pop(source_id, None)

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

        # A path is "active" this cycle only if every source referencing it
        # is within its plan's limit. If even one is backed off, the whole
        # path is skipped rather than reading it for its healthy sibling:
        # the offset is committed per-path (see below), so advancing it on
        # the sibling's success alone would silently skip past the backed-
        # off source's unprocessed share once its backoff clears. Skipping
        # the read entirely is also the actual point of backing off — a
        # source with no active path never gets its backlog re-parsed.
        path_sources: Dict[str, List[dict]] = {}
        for source in nginx_sources:
            for path in (source["access_log_path"], source["error_log_path"]):
                path_sources.setdefault(path, []).append(source)
        active_paths = {
            path
            for path, sources in path_sources.items()
            if not any(self._is_backed_off(s["id"]) for s in sources)
        }

        # Read each unique active file path exactly once per cycle. Reading
        # does not mutate self._file_positions yet — that only happens once
        # we know every consumer of a path succeeded.
        access_lines_by_path: Dict[str, Tuple[List[str], int]] = {}
        error_lines_by_path: Dict[str, Tuple[List[str], int]] = {}
        path_succeeded: Dict[str, bool] = {}

        for source in nginx_sources:
            for store, path in [
                (access_lines_by_path, source["access_log_path"]),
                (error_lines_by_path, source["error_log_path"]),
            ]:
                if path not in store and path in active_paths:
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
                self._clear_backoff(source_id)
                self.logger.debug(
                    f"Sent {len(events)} events to {endpoint} for source {source_id}"
                )
                return True

            if response.status_code == 429:
                self._register_quota_exceeded(source_id)
            else:
                self.logger.warning(
                    f"Failed to send {endpoint}: {response.status_code} - {response.text[:500]}"
                )
            return False
        except Exception as e:
            self.logger.error(f"Error sending {endpoint}: {e}")
            return False
