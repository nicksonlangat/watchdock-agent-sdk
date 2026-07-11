#!/usr/bin/env python3

import os
import time
import threading
import requests
import logging
import signal
import sys
import argparse
import platform
import socket
from datetime import datetime, timezone
from typing import List, Dict
from config import Config

AGENT_VERSION = "1.3.5"

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Warning: psutil not available. Server metrics collection will be disabled.")
    print("Install psutil with: pip install psutil")

try:
    from docker_monitor import DockerMonitor
    DOCKER_MONITOR_AVAILABLE = True
except ImportError:
    DOCKER_MONITOR_AVAILABLE = False
    print("Warning: Docker monitor not available.")

try:
    from container_log_collector import ContainerLogCollector
    CONTAINER_LOG_COLLECTOR_AVAILABLE = True
except ImportError:
    CONTAINER_LOG_COLLECTOR_AVAILABLE = False

try:
    from nginx_log_collector import NginxLogCollector
    NGINX_LOG_COLLECTOR_AVAILABLE = True
except ImportError:
    NGINX_LOG_COLLECTOR_AVAILABLE = False
    print("Warning: Nginx log collector not available.")

class ObservabilityAgent:
    def __init__(self):
        self.config = Config()
        self.running = False
        self._metrics_paused_until = 0

        # Initialize Docker monitor if available
        if DOCKER_MONITOR_AVAILABLE:
            self.docker_monitor = DockerMonitor(self.config)
        else:
            self.docker_monitor = None

        # Initialize container log collector if available
        if CONTAINER_LOG_COLLECTOR_AVAILABLE:
            self.container_log_collector = ContainerLogCollector(self.config)
        else:
            self.container_log_collector = None

        # Initialize nginx log collector if available
        if NGINX_LOG_COLLECTOR_AVAILABLE:
            self.nginx_log_collector = NginxLogCollector(self.config)
        else:
            self.nginx_log_collector = None

        logging.basicConfig(
            level=getattr(logging, self.config.get('log_level', 'INFO')),
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def start(self):
        if not self.config.validate():
            self.logger.error("Invalid configuration. Please check agent_config.json")
            sys.exit(1)

        self.config.fetch_server_config()

        self.running = True
        self.logger.info("Starting Observability Agent...")
        self._send_startup_event()

        # Start metrics collection thread (if enabled and psutil available)
        if self.config.get('collect_metrics', True) and PSUTIL_AVAILABLE:
            metrics_thread = threading.Thread(target=self._metrics_loop)
            metrics_thread.daemon = True
            metrics_thread.start()
            self.logger.info("Server metrics collection enabled")
        else:
            self.logger.info("Server metrics collection disabled")

        # Start Docker monitoring thread (if enabled and available)
        if (self.config.get('collect_docker_metrics', True) and
            DOCKER_MONITOR_AVAILABLE and self.docker_monitor and
            self.docker_monitor.docker_available):
            docker_thread = threading.Thread(target=self._docker_monitoring_loop)
            docker_thread.daemon = True
            docker_thread.start()
            self.logger.info("Docker container monitoring enabled")
        else:
            self.logger.info("Docker container monitoring disabled")

        # Start container log collection thread (if enabled and Docker available)
        if (self.config.get('collect_container_logs', True) and
            CONTAINER_LOG_COLLECTOR_AVAILABLE and self.container_log_collector):
            log_collector_thread = threading.Thread(target=self._container_log_collection_loop)
            log_collector_thread.daemon = True
            log_collector_thread.start()
            self.logger.info("Container log collection enabled")
        else:
            self.logger.info("Container log collection disabled")

        # Start nginx log collection thread (collect_and_send handles empty sources gracefully)
        if NGINX_LOG_COLLECTOR_AVAILABLE and self.nginx_log_collector:
            nginx_thread = threading.Thread(target=self._nginx_log_collection_loop)
            nginx_thread.daemon = True
            nginx_thread.start()
            self.logger.info("Nginx log collection enabled")
        else:
            self.logger.info("Nginx log collection disabled")

        if self.config.get('auto_update', False):
            self.logger.info("Auto-update enabled — will check for new versions on each config refresh")

        # Main loop
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
    
    def stop(self):
        self.logger.info("Stopping Observability Agent...")
        self.running = False
        self._send_shutdown_event("clean")
        self.logger.info("Agent stopped")

    def _collect_server_metrics(self) -> Dict:
        """Collect server metrics using psutil"""
        if not PSUTIL_AVAILABLE:
            return {}

        metrics = {
            'machine_id': self.config.get_machine_id(),
            'hostname': self.config.get_hostname(),
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'agent_version': AGENT_VERSION
        }

        try:
            # Network information
            try:
                # Get the actual server IP (the one that would be used for SSH)
                server_ip = self._get_server_ip()
                if server_ip:
                    metrics['ip_address'] = server_ip

                # Get public IP
                public_ip = self._get_public_ip()
                if public_ip:
                    metrics['public_ip'] = public_ip
            except Exception as e:
                self.logger.debug(f"Error collecting network info: {e}")

            # System information
            try:
                uname = platform.uname()
                boot_time = psutil.boot_time()
                uptime_seconds = int(time.time() - boot_time)

                metrics.update({
                    'os_name': uname.system,
                    'os_version': uname.release,
                    'kernel_version': uname.version,
                    'architecture': uname.machine,
                    'uptime_seconds': uptime_seconds
                })
            except Exception as e:
                self.logger.debug(f"Error collecting system info: {e}")

            # CPU metrics
            try:
                metrics.update({
                    'cpu_count': psutil.cpu_count(),
                    'cpu_usage_percent': psutil.cpu_percent(interval=1)
                })

                # Load averages (Unix-like systems only)
                try:
                    load_avg = psutil.getloadavg()
                    metrics.update({
                        'load_average_1m': load_avg[0],
                        'load_average_5m': load_avg[1],
                        'load_average_15m': load_avg[2]
                    })
                except (AttributeError, OSError):
                    pass
            except Exception as e:
                self.logger.debug(f"Error collecting CPU metrics: {e}")

            # Memory metrics
            try:
                memory = psutil.virtual_memory()
                metrics.update({
                    'memory_total': memory.total,
                    'memory_available': memory.available,
                    'memory_used': memory.used,
                    'memory_usage_percent': memory.percent
                })
            except Exception as e:
                self.logger.debug(f"Error collecting memory metrics: {e}")

            # Swap metrics
            try:
                swap = psutil.swap_memory()
                metrics.update({
                    'swap_total': swap.total,
                    'swap_used': swap.used,
                    'swap_usage_percent': swap.percent
                })
            except Exception as e:
                self.logger.debug(f"Error collecting swap metrics: {e}")

            # Disk metrics
            try:
                # Use root directory, or C:\ on Windows
                path = 'C:\\' if platform.system() == 'Windows' else '/'
                disk = psutil.disk_usage(path)
                metrics.update({
                    'disk_total': disk.total,
                    'disk_used': disk.used,
                    'disk_available': disk.free,
                    'disk_usage_percent': (disk.used / disk.total) * 100 if disk.total > 0 else 0
                })
            except Exception as e:
                self.logger.debug(f"Error collecting disk metrics: {e}")

            # Process and network metrics
            try:
                metrics['process_count'] = len(psutil.pids())

                net_io = psutil.net_io_counters()
                metrics.update({
                    'network_bytes_sent': net_io.bytes_sent,
                    'network_bytes_received': net_io.bytes_recv
                })
            except Exception as e:
                self.logger.debug(f"Error collecting process/network metrics: {e}")

        except Exception as e:
            self.logger.error(f"Error collecting server metrics: {e}")

        return metrics

    def _send_server_metrics(self):
        """Send server metrics to the API"""
        if time.time() < self._metrics_paused_until:
            self.logger.debug("Server metrics paused due to plan limit, skipping")
            return

        try:
            metrics = self._collect_server_metrics()
            if not metrics:
                return

            api_token = self.config.get("api_token")
            response = requests.post(
                f"{self.config.get('api_endpoint')}/core/agent/metrics/",
                json=metrics,
                headers={
                    'Authorization': f'Bearer {api_token}',
                    'Content-Type': 'application/json'
                },
                timeout=30
            )

            if response.status_code == 201:
                self.logger.debug("Server metrics sent successfully")
            elif response.status_code == 429:
                self._metrics_paused_until = time.time() + 3600
                self.logger.warning("Server metrics paused for 1 hour — plan limit reached")
            else:
                self.logger.warning(f"Failed to send metrics: {response.status_code} - {response.text}")

        except Exception as e:
            self.logger.error(f"Error sending server metrics: {e}")

    def _metrics_loop(self):
        """Periodically send server metrics"""
        self.logger.info("Starting metrics collection loop")

        while self.running:
            self.config.fetch_server_config()
            try:
                self._send_server_metrics()
            except Exception as e:
                self.logger.error(f"Error in metrics loop: {e}")
            if self.config.get('auto_update', False):
                self._check_for_update()
            time.sleep(self.config.get('metrics_interval', 300))

    def _get_server_ip(self) -> str:
        """Get the actual server IP address that would be used for external connections"""
        try:
            # Method 1: Connect to a remote address to see which local IP is used
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                # Use Google's DNS server as target (doesn't actually send data)
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except:
            pass

        try:
            # Method 2: Get all network interfaces and find the first non-loopback IP
            if PSUTIL_AVAILABLE:
                for interface, addrs in psutil.net_if_addrs().items():
                    for addr in addrs:
                        if (addr.family == socket.AF_INET and
                            not addr.address.startswith('127.') and
                            not addr.address.startswith('169.254.')):  # Skip loopback and link-local
                            return addr.address
        except:
            pass

        try:
            # Method 3: Fallback to hostname resolution
            hostname = socket.gethostname()
            return socket.gethostbyname(hostname)
        except:
            return None

    def _get_public_ip(self) -> str:
        """Get the public IP address using external services"""
        services = [
            'https://api.ipify.org?format=text',
            'https://checkip.amazonaws.com',
            'https://ipecho.net/plain',
            'https://icanhazip.com'
        ]

        for service in services:
            try:
                response = requests.get(service, timeout=5)
                if response.status_code == 200:
                    ip = response.text.strip()
                    # Basic validation that it looks like an IP
                    if len(ip.split('.')) == 4:
                        return ip
            except:
                continue

        return None

    def _docker_monitoring_loop(self):
        """Periodically collect and send Docker container metrics"""
        self.logger.info("Starting Docker monitoring loop")

        while self.running:
            self.config.fetch_server_config()
            try:
                containers = self.docker_monitor.collect_all_containers()
                if containers:
                    self.docker_monitor.send_container_metrics(containers)
                    self.logger.debug(f"Sent metrics for {len(containers)} containers")
            except Exception as e:
                self.logger.error(f"Error in Docker monitoring loop: {e}")
            time.sleep(self.config.get('docker_metrics_interval', 60))

    def _container_log_collection_loop(self):
        """Periodically collect and send Docker container logs"""
        self.logger.info("Starting container log collection loop")
        while self.running:
            self.config.fetch_server_config()
            if not self.config.get('collect_container_logs', True):
                time.sleep(self.config.get('container_log_interval', 30))
                continue
            try:
                logs = self.container_log_collector.collect_logs()
                if logs:
                    status_code = self.container_log_collector.send_logs(logs)
                    if status_code == 403:
                        self.config.config['collect_container_logs'] = False
                        self.logger.info(
                            "Container log collection disabled — upgrade your plan to access Docker container logs"
                        )
                    else:
                        self.logger.debug(f"Sent {len(logs)} container log entries")
            except Exception as e:
                self.logger.error(f"Error in container log collection loop: {e}")
            time.sleep(self.config.get('container_log_interval', 30))

    def _nginx_log_collection_loop(self):
        """Periodically collect and send nginx access metrics and error events"""
        self.logger.info("Starting nginx log collection loop")

        while self.running:
            self.config.fetch_server_config()
            try:
                self.nginx_log_collector.collect_and_send()
            except Exception as e:
                self.logger.error(f"Error in nginx log collection loop: {e}")
            time.sleep(self.config.get('nginx_interval', 60))

    def _check_for_update(self):
        """Check if a newer agent version is available and self-update if so."""
        try:
            resp = requests.get(
                f"{self.config.get('api_endpoint')}/platform-stats/",
                timeout=10,
            )
            if resp.status_code != 200:
                return
            latest = resp.json().get("latest_agent_version")
            if latest and latest != AGENT_VERSION:
                self.logger.info(f"New agent version available: {latest} (current: {AGENT_VERSION}). Updating...")
                self._perform_update()
            else:
                self.logger.debug(f"Agent is up to date (v{AGENT_VERSION})")
        except Exception as e:
            self.logger.warning(f"Auto-update check failed: {e}")

    def _perform_update(self):
        """
        Download the upgrade script and launch it in a detached systemd scope.

        The upgrade script's own first step is `systemctl stop watchdock-agent`.
        This process runs *inside* that service's cgroup (KillMode=control-group
        is the systemd default), so running the script as a direct child
        subprocess would kill it mid-flight the instant it issues that stop —
        it never gets to extract the new files or start the service again.
        `systemd-run` launches it as an independent transient unit with its own
        cgroup, so it survives being orphaned when this service (and this
        process) goes down. We deliberately don't wait for the upgrade itself
        to finish — only for `systemd-run` to confirm it launched — since this
        process won't be alive to see the result either way.
        """
        import subprocess
        import tempfile

        # Strip only a trailing "/api" path segment. A naive .replace("/api", "")
        # also matches the "/api" hiding inside "https://api.watchdock.cc" (the
        # second slash of "//" plus the "api" subdomain), mangling the host
        # entirely — e.g. producing "https:/.watchdock.cc" with no host at all.
        api_endpoint = self.config.get('api_endpoint', '').rstrip('/')
        base_url = api_endpoint[: -len('/api')] if api_endpoint.endswith('/api') else api_endpoint
        upgrade_url = f"{base_url}/upgrade.sh"
        try:
            resp = requests.get(upgrade_url, timeout=15)
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                f.write(resp.text)
                script_path = f.name
            import os
            os.chmod(script_path, 0o755)
            result = subprocess.run(
                [
                    "systemd-run",
                    "--unit=watchdock-agent-upgrade",
                    "--collect",
                    "--",
                    "bash",
                    script_path,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                self.logger.info(
                    "Auto-update launched in a detached scope — this process and the "
                    "old service will stop shortly; the new version starts automatically"
                )
            else:
                self.logger.error(f"Failed to launch auto-update: {result.stderr[:500]}")
        except Exception as e:
            self.logger.error(f"Auto-update failed: {e}")

    def _send_startup_event(self):
        """Notify the platform immediately that this agent has started."""
        try:
            api_token = self.config.get("api_token")
            response = requests.post(
                f"{self.config.get('api_endpoint')}/core/agent/startup/",
                json={
                    "machine_id": self.config.get_machine_id(),
                    "hostname": self.config.get_hostname(),
                    "agent_version": AGENT_VERSION,
                },
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if response.status_code == 200:
                self.logger.info("Startup event recorded by platform")
            else:
                self.logger.warning(f"Startup event returned {response.status_code}")
        except Exception as e:
            self.logger.warning(f"Could not send startup event: {e}")

    def _send_shutdown_event(self, reason: str = "clean"):
        """Notify the platform that this agent is shutting down cleanly."""
        try:
            api_token = self.config.get("api_token")
            requests.post(
                f"{self.config.get('api_endpoint')}/core/agent/shutdown/",
                json={
                    "machine_id": self.config.get_machine_id(),
                    "hostname": self.config.get_hostname(),
                    "reason": reason,
                },
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                timeout=5,
            )
        except Exception as e:
            self.logger.debug(f"Could not send shutdown event: {e}")

    def _signal_handler(self, signum, frame):
        self.logger.info(f"Received signal {signum}")
        self._send_shutdown_event("signal")
        self.stop()

def test_configuration():
    """Test agent configuration and connectivity"""
    try:
        # Test configuration loading
        config = Config()
        print("✓ Configuration file loaded successfully")

        # Test required fields (log_source_id no longer required - using auto-discovery)
        required_fields = ['api_endpoint', 'api_token']
        for field in required_fields:
            if not config.get(field):
                print(f"✗ Missing required field: {field}")
                return False
        print("✓ All required fields present")

        # Show machine identification
        print(f"  Machine ID: {config.get_machine_id()}")
        print(f"  Hostname: {config.get_hostname()}")

        # Test API connectivity by sending a test metric
        print("Testing API connectivity...")
        api_token = config.get("api_token")

        # Test with a simple metrics payload (auto-discovery will create log source)
        test_payload = {
            'machine_id': config.get_machine_id(),
            'hostname': config.get_hostname(),
            'collected_at': datetime.now(timezone.utc).isoformat(),
            'cpu_usage_percent': 0,
            'memory_usage_percent': 0,
            'disk_usage_percent': 0
        }

        response = requests.post(
            f"{config.get('api_endpoint')}/core/agent/metrics/",
            headers={'Authorization': f'Bearer {api_token}'},
            json=test_payload,
            timeout=10
        )

        # If that fails with 401, try query parameter approach
        if response.status_code == 401:
            print("Trying query parameter authentication...")
            response = requests.post(
                f"{config.get('api_endpoint')}/core/agent/metrics/",
                params={'api_key': api_token},
                json=test_payload,
                timeout=10
            )

        if response.status_code == 200:
            print("✓ API connection successful")
        else:
            print(f"✗ API connection failed: {response.status_code}")
            return False

        print("✓ Configuration test passed")
        return True

    except Exception as e:
        print(f"✗ Configuration test failed: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description='WatchDock Agent')
    parser.add_argument('--test-config', action='store_true',
                       help='Test configuration and exit')
    parser.add_argument('--config', default='agent_config.json',
                       help='Configuration file path (default: agent_config.json)')

    args = parser.parse_args()

    if args.test_config:
        success = test_configuration()
        sys.exit(0 if success else 1)

    # Normal operation
    agent = ObservabilityAgent()
    agent.start()

if __name__ == "__main__":
    main()