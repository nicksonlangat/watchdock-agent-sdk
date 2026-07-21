#!/bin/bash
# One-line installer for WatchDock Agent
# Usage: curl -sSL https://api.watchdock.cc/install.sh | WATCHDOCK_API_TOKEN=pos_xxx sudo -E bash

set -e

API_TOKEN="${1}"
VERSION="${2:-latest}"
REPO_URL="https://github.com/nicksonlangat/platform_obs_agent"
DOWNLOAD_URL="${REPO_URL}/releases/download/agent-v${VERSION}/watchdock-agent-${VERSION}.tar.gz"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[INSTALL]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Check root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root. Use: sudo bash -s YOUR_API_TOKEN"
fi

# Check API token
if [[ -z "$API_TOKEN" ]]; then
    error "API token is required. Usage: bash -s YOUR_API_TOKEN"
fi

# Show banner
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}        WatchDock Agent - Quick Installer${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"
echo

log "Downloading agent version: $VERSION"
log "Installing to: /opt/watchdock-agent"
echo

# Create temp directory
TEMP_DIR=$(mktemp -d)
cd "$TEMP_DIR"

# Download
if [[ "$VERSION" == "latest" ]]; then
    DOWNLOAD_URL="${REPO_URL}/releases/latest/download/watchdock-agent-latest.tar.gz"
fi

log "Downloading from: $DOWNLOAD_URL"
if ! curl -sSL "$DOWNLOAD_URL" -o agent.tar.gz; then
    error "Failed to download agent. Check URL: $DOWNLOAD_URL"
fi

# Extract
log "Extracting..."
tar -xzf agent.tar.gz
cd watchdock-agent-*/

# Create config
log "Creating configuration..."
# Give every fresh install its own random identity so that a box cloned from a
# snapshot cannot share another server's machine-id. Existing installs keep
# their identity via the agent's own adoption logic (see config.get_machine_id).
if [[ -r /proc/sys/kernel/random/uuid ]]; then
  AGENT_ID="$(cat /proc/sys/kernel/random/uuid)"
elif command -v uuidgen >/dev/null 2>&1; then
  AGENT_ID="$(uuidgen)"
else
  AGENT_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
fi
cat > agent_config.json << EOF
{
  "api_endpoint": "https://api.watchdock.cc/api",
  "api_token": "${API_TOKEN}",
  "agent_id": "${AGENT_ID}",
  "log_files": [],
  "collect_metrics": true,
  "metrics_interval": 300,
  "collect_docker_metrics": true,
  "docker_metrics_interval": 60,
  "collect_http_checks": false,
  "log_level": "INFO"
}
EOF

# Install
log "Installing agent..."
chmod +x install.sh
./install.sh

# Cleanup
cd /
rm -rf "$TEMP_DIR"

# Show status
echo
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✓ Installation complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo
log "Service status:"
systemctl status watchdock-agent --no-pager | head -n 3
echo
log "View logs: journalctl -u watchdock-agent -f"
log "Manage: sudo systemctl [start|stop|restart|status] watchdock-agent"
echo
echo -e "${GREEN}Your server will appear in the dashboard within 60 seconds!${NC}"
echo
