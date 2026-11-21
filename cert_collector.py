"""
Reports the TLS certificates on this box and what renews them, so an expiry
incident can say which box serves the domain and what to run there.

Reads certificate metadata only (domains, expiry, renewal config). Private keys
are never opened: every path read here is a certificate or a config file.
"""

import configparser
import glob
import logging
import os
import re
import ssl
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

LETSENCRYPT_RENEWAL = "/etc/letsencrypt/renewal"
LETSENCRYPT_LOG = "/var/log/letsencrypt/letsencrypt.log"
GLOBAL_HOOK_DIRS = ("/etc/letsencrypt/renewal-hooks/deploy", "/etc/letsencrypt/renewal-hooks/post")
NGINX_CONF_GLOBS = (
    "/etc/nginx/nginx.conf",
    "/etc/nginx/sites-enabled/*",
    "/etc/nginx/conf.d/*.conf",
)
CADDY_CERT_GLOB = "/var/lib/caddy/.local/share/caddy/certificates/*/*/*.crt"
ACME_SH_DIRS = ("/etc/acme.sh", "/root/.acme.sh")
CERTBOT_TIMERS = ("certbot.timer", "snap.certbot.renew.timer")
CERTBOT_CRON = ("/etc/cron.d/certbot", "/etc/crontab")

SSL_CERT_RE = re.compile(r"^\s*ssl_certificate\s+([^;\s]+)\s*;", re.MULTILINE)
SERVER_NAME_RE = re.compile(r"^\s*server_name\s+([^;]+);", re.MULTILINE)
LOG_FAILURE_RE = re.compile(r"(Failed to renew certificate.*|Detail:.*|Challenge failed.*)")
MAX_CERTS = 200


def decode_cert(path: str) -> Optional[Dict]:
    """Domains and expiry of the first certificate in a PEM file."""
    try:
        info = ssl._ssl._test_decode_cert(path)
    except Exception:
        return None
    domains = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"]
    if not domains:
        domains = [v for rdn in info.get("subject", ()) for k, v in rdn if k == "commonName"]
    not_after = info.get("notAfter")
    if not not_after:
        return None
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    issuer = next((v for rdn in info.get("issuer", ()) for k, v in rdn if k in ("organizationName", "commonName")), "")
    return {"domains": sorted(set(domains)), "not_after": expires.isoformat(), "issuer": issuer}


def run(args: List[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def certbot_schedule() -> Dict:
    for timer in CERTBOT_TIMERS:
        state = run(["systemctl", "is-enabled", timer])
        if state and state != "not-found" and "No such file" not in state:
            service = timer.replace(".timer", ".service")
            props = dict(
                line.split("=", 1)
                for line in run(["systemctl", "show", service, "-p", "Result", "-p", "ExecMainExitTimestamp"]).splitlines()
                if "=" in line
            )
            return {
                "kind": "systemd",
                "unit": timer,
                "enabled": state == "enabled",
                "active": run(["systemctl", "is-active", timer]) == "active",
                "last_result": props.get("Result") or None,
                "last_run": props.get("ExecMainExitTimestamp") or None,
            }
    for path in CERTBOT_CRON:
        try:
            with open(path) as f:
                if "certbot" in f.read():
                    return {"kind": "cron", "unit": path, "enabled": True}
        except OSError:
            continue
    return {"kind": None, "enabled": False}


def certbot_last_error() -> Optional[str]:
    """Most recent failure line from certbot's log, if the last run failed."""
    try:
        with open(LETSENCRYPT_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    matches = LOG_FAILURE_RE.findall(tail)
    return matches[-1][:300] if matches else None


def none_if_unset(value: Optional[str]) -> Optional[str]:
    return None if value in (None, "", "None") else value


def certbot_certs() -> List[Dict]:
    global_hook = any(os.listdir(d) for d in GLOBAL_HOOK_DIRS if os.path.isdir(d))
    certs = []
    for conf_path in sorted(glob.glob(os.path.join(LETSENCRYPT_RENEWAL, "*.conf"))):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with open(conf_path) as f:
                parser.read_string("[top]\n" + f.read())
        except (OSError, configparser.Error):
            continue
        top = parser["top"]
        params = parser["renewalparams"] if parser.has_section("renewalparams") else {}
        cert_path = top.get("cert") or top.get("fullchain")
        meta = decode_cert(cert_path) if cert_path else None
        if not meta:
            continue
        certs.append({
            **meta,
            "name": os.path.basename(conf_path)[: -len(".conf")],
            "path": cert_path,
            "renewer": "certbot",
            "authenticator": none_if_unset(params.get("authenticator")),
            "installer": none_if_unset(params.get("installer")),
            "has_deploy_hook": global_hook
            or bool(params.get("renew_hook") or params.get("deploy_hook") or params.get("post_hook")),
        })
    return certs


def nginx_certs() -> List[Dict]:
    """Certificates nginx points at, with the server_names that use them."""
    by_path: Dict[str, set] = {}
    for pattern in NGINX_CONF_GLOBS:
        for conf in glob.glob(pattern):
            try:
                with open(conf) as f:
                    text = f.read()
            except OSError:
                continue
            for block in re.split(r"^\s*server\s*\{", text, flags=re.MULTILINE)[1:]:
                names = {n for m in SERVER_NAME_RE.findall(block) for n in m.split() if n != "_"}
                for path in SSL_CERT_RE.findall(block):
                    by_path.setdefault(path, set()).update(names)
    certs = []
    for path, names in by_path.items():
        meta = decode_cert(path)
        if meta:
            certs.append({**meta, "path": path, "server_names": sorted(names), "served_by": "nginx"})
    return certs


def caddy_certs() -> List[Dict]:
    certs = []
    for path in glob.glob(CADDY_CERT_GLOB):
        meta = decode_cert(path)
        if meta:
            certs.append({**meta, "name": os.path.basename(path)[:-4], "path": path, "renewer": "caddy"})
    return certs


def acme_sh_certs() -> List[Dict]:
    certs = []
    for base in ACME_SH_DIRS:
        for path in glob.glob(os.path.join(base, "*", "*.cer")):
            name = os.path.basename(os.path.dirname(path))
            if os.path.basename(path) != f"{name}.cer":
                continue
            meta = decode_cert(path)
            if meta:
                certs.append({**meta, "name": name, "path": path, "renewer": "acme.sh"})
    return certs


def collect() -> Dict:
    managed = certbot_certs() + caddy_certs() + acme_sh_certs()
    by_path = {c["path"]: c for c in managed}
    certs = list(managed)
    for served in nginx_certs():
        real = os.path.realpath(served["path"])
        match = by_path.get(served["path"]) or next(
            (c for c in managed if os.path.dirname(os.path.realpath(c["path"])) == os.path.dirname(real)), None
        )
        if match:
            match["served_by"] = "nginx"
            match["server_names"] = served["server_names"]
        else:
            certs.append({**served, "renewer": "unknown"})

    renewers = {}
    if any(c["renewer"] == "certbot" for c in certs):
        renewers["certbot"] = {"schedule": certbot_schedule(), "last_error": certbot_last_error()}
    return {"certificates": certs[:MAX_CERTS], "renewers": renewers}


class CertCollector:
    def __init__(self, config):
        self.config = config

    def collect_and_send(self) -> None:
        report = collect()
        payload = {
            "machine_id": self.config.get_machine_id(),
            "hostname": self.config.get_hostname(),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            **report,
        }
        resp = requests.post(
            f"{self.config.get('api_endpoint')}/core/agent/certificates/",
            json=payload,
            headers={"Authorization": f"Bearer {self.config.get('api_token')}"},
            timeout=30,
        )
        if resp.status_code == 404:
            logger.debug("Certificate reporting not supported by this API yet")
        elif resp.status_code >= 400:
            logger.warning(f"Failed to send certificates: {resp.status_code} - {resp.text[:200]}")
        else:
            logger.debug(f"Sent {len(report['certificates'])} certificates")
