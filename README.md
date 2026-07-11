# WatchDock Agent

A lightweight Python agent that runs on your server to collect and forward system metrics, Docker container data, and log entries to the [WatchDock](https://watchdock.cc) dashboard.

## Install

```bash
curl -fsSL https://api.watchdock.cc/install.sh | WATCHDOCK_API_TOKEN=pos_your_token_here sudo -E bash
```

Get your API token from the WatchDock dashboard under **Settings → API Token**.

Your server will appear in the dashboard within 60 seconds.

---

## What It Monitors

### Server Metrics (via psutil)
- CPU usage, core count, load averages (1m/5m/15m)
- Memory and swap usage
- Disk usage (root partition)
- Network bytes sent/received
- Process count, uptime, OS info, public/private IP

### Docker Containers (auto-discovered)
- Automatically discovers all containers on the host — no config needed
- Per container: status, health, CPU %, memory usage/limit, network I/O, block I/O, PIDs
- Restart count, exit codes, OOMKilled detection
- Uptime, start/finish timestamps
- Container logs: errors and tracebacks collected automatically

### Log File Monitoring
- Tails configured log files in real time
- Parses timestamps and log levels automatically
- Batched delivery for efficiency
- Handles log rotation gracefully

### Nginx Access/Error Logs
- Tails nginx access and error logs using the `watchdock` log format
- Captures nginx's built-in `$request_id` per request when present in the format, letting the dashboard correlate a request with the exact `watchdock-errors` SDK event it produced (see [docs](https://watchdock.cc/docs/nginx-log-collection))

---

## Configuration

Config file lives at `/opt/watchdock-agent/agent_config.json` and is created automatically during installation. Only the API token is required — everything else has sensible defaults.

```json
{
  "api_endpoint": "https://api.watchdock.cc/api",
  "api_token": "pos_your_token_here",
  "log_files": [],
  "heartbeat_interval": 60,
  "log_level": "INFO",
  "collect_metrics": true,
  "metrics_interval": 300,
  "collect_docker_metrics": true,
  "docker_metrics_interval": 60,
  "collect_container_logs": true,
  "container_log_interval": 30,
  "container_log_max_lines": 500
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `api_token` | (required) | Your organization API token from the dashboard |
| `log_files` | `[]` | Log file paths to tail |
| `heartbeat_interval` | `60` | Seconds between heartbeats |
| `metrics_interval` | `300` | Seconds between server metric collections |
| `docker_metrics_interval` | `60` | Seconds between Docker metric collections |
| `container_log_interval` | `30` | Seconds between container log collections |
| `container_log_max_lines` | `500` | Max log lines fetched per container per cycle |
| `log_level` | `INFO` | Agent log verbosity: DEBUG, INFO, WARNING, ERROR |

After editing the config, restart the agent:

```bash
sudo systemctl restart watchdock-agent
```

---

## Upgrade

```bash
curl -fsSL https://api.watchdock.cc/upgrade.sh | sudo bash
```

Your existing configuration is preserved automatically.

---

## Management

```bash
systemctl status watchdock-agent       # Check status
systemctl restart watchdock-agent      # Restart
journalctl -u watchdock-agent -f       # Live logs
cat /opt/watchdock-agent/VERSION       # Installed version
```

---

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Main agent — daemon threads and orchestration |
| `config.py` | Config loader, validation, and remote config fetch |
| `log_parser.py` | Log line parser (timestamp, level extraction) |
| `docker_monitor.py` | Docker container discovery and metrics |
| `container_log_collector.py` | Docker container log collection |
| `requirements.txt` | Python dependencies |
| `install.sh` | Installation script |
| `upgrade.sh` | Upgrade script |

---

## Requirements

- Linux (Ubuntu, Debian, CentOS, RHEL, Amazon Linux — any systemd-based distro)
- Python 3.8+
- Root access
- Docker (optional, for container monitoring)
