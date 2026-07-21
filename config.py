import json
import logging
import os
import socket
import time
import uuid
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

AGENT_CONFIG_KEYS = (
    "metrics_interval",
    "docker_metrics_interval",
    "container_log_interval",
    "collect_container_logs",
    "nginx_interval",
    "nginx_sources",
    "auto_update",
)

MIN_REFRESH_INTERVAL = 30  # seconds — never hit the API more often than this


class Config:
    def __init__(self, config_file: str = "agent_config.json"):
        self.config_file = config_file
        self.config = self._load_config()
        self._machine_id = None
        self._hostname = None
        self._last_fetched: float = 0

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                return json.load(f)

        default_config = {
            "api_endpoint": "https://api.watchdock.cc/api",
            "api_token": "",
            "log_level": "INFO",
            "collect_metrics": True,
            "metrics_interval": 300,
            "collect_docker_metrics": True,
            "docker_metrics_interval": 60,
            "collect_container_logs": True,
            "container_log_interval": 30,
            "container_log_max_lines": 500,
            "nginx_interval": 60,
            "nginx_sources": []
        }

        self._save_config(default_config)
        return default_config

    def _save_config(self, config: Dict[str, Any]):
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=2)

    def get(self, key: str, default=None):
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value
        self._save_config(self.config)

    def get_machine_id(self) -> str:
        """
        Return this agent's stable identity, used server-side as the LogSource key.

        Resolution order:
        1. agent_id persisted in the config. Once set, this is authoritative and
           never changes, even if /etc/machine-id or the hostname later change.
        2. Otherwise resolve the legacy identity exactly as older agents did
           (/etc/machine-id, then /var/lib/dbus/machine-id, then a uuid5 of the
           hostname) and persist it as agent_id.

        Branch 2 is what makes upgrades lossless: an existing agent keeps reporting
        the same string it always has, so its server-side record is preserved
        rather than split into a new one. Fresh installs are seeded with a random
        agent_id by the installer, so they take branch 1 and never collide with a
        cloned machine-id.
        """
        if self._machine_id:
            return self._machine_id

        stored = self.config.get("agent_id")
        if stored:
            self._machine_id = stored
            return self._machine_id

        self._machine_id = self._resolve_legacy_machine_id()
        try:
            self.set("agent_id", self._machine_id)  # persist so identity is pinned
        except OSError as exc:
            logger.warning("Could not persist agent_id to config: %s", exc)
        return self._machine_id

    def _resolve_legacy_machine_id(self) -> str:
        """Reproduce the historical identity derivation for existing installs."""
        for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                with open(path, 'r') as f:
                    value = f.read().strip()
                    if value:
                        return value
            except (FileNotFoundError, PermissionError):
                continue

        # Fallback: stable ID from hostname (matches older agent behaviour).
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, self.get_hostname()))

    def get_hostname(self) -> str:
        """Get the system hostname"""
        if self._hostname:
            return self._hostname

        # Check config first
        hostname = self.config.get('hostname')
        if hostname:
            self._hostname = hostname
            return hostname

        # Get from system
        try:
            self._hostname = socket.gethostname()
        except Exception:
            self._hostname = 'unknown-host'

        return self._hostname

    def fetch_server_config(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and (now - self._last_fetched) < MIN_REFRESH_INTERVAL:
            return True  # Still fresh, skip

        api_endpoint = self.config.get("api_endpoint", "").rstrip("/")
        api_token = self.config.get("api_token", "")
        if not api_endpoint or not api_token:
            return False

        try:
            response = requests.get(
                f"{api_endpoint}/agent/config/",
                headers={"Authorization": f"Bearer {api_token}"},
                params={"machine_id": self.get_machine_id()},
                timeout=10,
            )
            response.raise_for_status()
            server_config = response.json()

            updated = False
            for key in AGENT_CONFIG_KEYS:
                if key in server_config:
                    self.config[key] = server_config[key]
                    updated = True

            if updated:
                self._save_config(self.config)
                logger.info("Agent config updated from server (plan: %s)", server_config.get("plan", "unknown"))

            self._last_fetched = time.monotonic()
            return True

        except Exception as exc:
            logger.warning("Could not fetch server config, using local defaults: %s", exc)
            return False

    def validate(self) -> bool:
        """Validate only api_token is required now (auto-discovery handles the rest)"""
        required_fields = ["api_endpoint", "api_token"]
        for field in required_fields:
            if not self.config.get(field):
                print(f"Missing required configuration: {field}")
                return False
        return True