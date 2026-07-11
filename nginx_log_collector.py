#!/usr/bin/env python3
"""
Nginx log collector.
Tails nginx access and error log files, parses lines using the watchdock log format,
and sends raw per-request access events and error events to WatchDock.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
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


class NginxLogCollector:
    """
    Reads nginx access and error log files for each configured NginxLogSource,
    parses new lines since the last collection, and sends raw per-request events
    and error events to the WatchDock backend.
    """

    def __init__(self, config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        # Byte offsets keyed by file path. Each unique path is read once per
        # cycle and its lines distributed to all sources that reference it.
        self._file_positions: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def collect_and_send(self) -> None:
        """
        Called once per nginx_interval. For every configured NginxLogSource,
        reads new lines from its access and error log files, parses them,
        and sends to the backend.
        """
        nginx_sources = self.config.get("nginx_sources", [])
        if not nginx_sources:
            return

        # Read each unique file path exactly once per cycle.
        access_lines_by_path: Dict[str, List[str]] = {}
        error_lines_by_path: Dict[str, List[str]] = {}

        for source in nginx_sources:
            for store, path in [
                (access_lines_by_path, source["access_log_path"]),
                (error_lines_by_path, source["error_log_path"]),
            ]:
                if path not in store:
                    store[path] = self._read_new_lines(path)

        # Process each source with its collected lines.
        for source in nginx_sources:
            source_id = source["id"]
            prefix = source.get("filter_path_prefix", "")

            access_lines = access_lines_by_path.get(source["access_log_path"], [])
            events = self._parse_access_lines(access_lines, prefix)
            if events:
                self._send_access_events(source_id, events)

            error_lines = error_lines_by_path.get(source["error_log_path"], [])
            error_events = self._parse_error_lines(error_lines)
            if error_events:
                self._send_error_events(source_id, error_events)

    # ------------------------------------------------------------------
    # File reading
    # ------------------------------------------------------------------

    def _read_new_lines(self, path: str) -> List[str]:
        """
        Read new lines from a log file since the last recorded offset.
        Handles log rotation by resetting to offset 0 when the file shrinks.
        Returns an empty list if the file does not exist or cannot be read.
        """
        if not os.path.exists(path):
            return []

        try:
            current_size = os.path.getsize(path)
            last_position = self._file_positions.get(path, 0)

            # Log rotation: file is smaller than our last position.
            if current_size < last_position:
                self.logger.info(f"Nginx log rotation detected: {path}")
                last_position = 0

            # No new content.
            if current_size <= last_position:
                return []

            lines: List[str] = []
            with open(path, "r", errors="replace") as f:
                f.seek(last_position)
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)
                self._file_positions[path] = f.tell()

            return lines

        except Exception as e:
            self.logger.error(f"Error reading nginx log file {path}: {e}")
            return []

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

    def _send_access_events(self, source_id: str, events: List[dict]) -> None:
        """POST raw per-request access events to the backend."""
        try:
            response = requests.post(
                f"{self.config.get('api_endpoint')}/core/agent/nginx-access-events/",
                json={"nginx_log_source_id": source_id, "events": events},
                headers={
                    "Authorization": f"Bearer {self.config.get('api_token')}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if response.status_code in (200, 201):
                self.logger.debug(
                    f"Nginx access events sent: {len(events)} events for source {source_id}"
                )
            else:
                self.logger.warning(
                    f"Failed to send nginx access events: {response.status_code} - {response.text}"
                )
        except Exception as e:
            self.logger.error(f"Error sending nginx access events: {e}")

    def _send_error_events(self, source_id: str, events: List[dict]) -> None:
        """POST parsed error events to the backend."""
        try:
            response = requests.post(
                f"{self.config.get('api_endpoint')}/core/agent/nginx-error-events/",
                json={"nginx_log_source_id": source_id, "events": events},
                headers={
                    "Authorization": f"Bearer {self.config.get('api_token')}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if response.status_code in (200, 201):
                self.logger.debug(
                    f"Nginx error events sent: {len(events)} events for source {source_id}"
                )
            else:
                self.logger.warning(
                    f"Failed to send nginx error events: {response.status_code} - {response.text}"
                )
        except Exception as e:
            self.logger.error(f"Error sending nginx error events: {e}")
