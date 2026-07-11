#!/bin/bash

# WatchDock Agent - Upgrade Script
# Usage: sudo ./upgrade.sh [version]
# Example: sudo ./upgrade.sh 1.0.1

set -e

VERSION="${1:-latest}"
AGENT_DIR="/opt/watchdock-agent"
SERVICE_NAME="watchdock-agent"
BACKUP_DIR="/opt/watchdock-agent-backup-$(date +%Y%m%d-%H%M%S)"
REPO_URL="https://github.com/nicksonlangat/platform_obs_agent"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[UPGRADE]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Check root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root. Use: sudo ./upgrade.sh"
fi

# Show banner
echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}          WatchDock Agent - Upgrade Tool${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo

# Check if agent is installed
if [[ ! -d "$AGENT_DIR" ]]; then
    error "Agent not found at $AGENT_DIR. Please run install.sh first."
fi

# Get current version
CURRENT_VERSION="unknown"
if [[ -f "$AGENT_DIR/VERSION" ]]; then
    CURRENT_VERSION=$(cat "$AGENT_DIR/VERSION")
fi

log "Current version: $CURRENT_VERSION"
log "Upgrading to: $VERSION"
echo

# Stop the service
log "Stopping agent service..."
systemctl stop "$SERVICE_NAME" 2>/dev/null || warning "Service not running"

# Backup current installation
log "Creating backup at: $BACKUP_DIR"
cp -r "$AGENT_DIR" "$BACKUP_DIR"

# Backup config specifically
CONFIG_BACKUP="/tmp/agent_config_backup.json"
if [[ -f "$AGENT_DIR/agent_config.json" ]]; then
    cp "$AGENT_DIR/agent_config.json" "$CONFIG_BACKUP"
    log "Config backed up to: $CONFIG_BACKUP"
else
    warning "No config file found to backup"
fi

# Download new version
log "Downloading version $VERSION..."
TEMP_DIR=$(mktemp -d)
cd "$TEMP_DIR"

if [[ "$VERSION" == "latest" ]]; then
    DOWNLOAD_URL="${REPO_URL}/releases/latest/download/watchdock-agent-latest.tar.gz"
else
    DOWNLOAD_URL="${REPO_URL}/releases/download/agent-v${VERSION}/watchdock-agent-${VERSION}.tar.gz"
fi

if ! curl -sSL "$DOWNLOAD_URL" -o agent.tar.gz; then
    error "Failed to download version $VERSION from: $DOWNLOAD_URL"
fi

# Extract
log "Extracting new version..."
tar -xzf agent.tar.gz
cd watchdock-agent-*/

# Get new version number
NEW_VERSION="$VERSION"
if [[ -f "VERSION" ]]; then
    NEW_VERSION=$(cat VERSION)
fi

# Update files (preserve config)
log "Updating agent files..."
cp agent.py "$AGENT_DIR/"
cp config.py "$AGENT_DIR/"
cp log_parser.py "$AGENT_DIR/"
cp docker_monitor.py "$AGENT_DIR/" 2>/dev/null || true
cp container_log_collector.py "$AGENT_DIR/" 2>/dev/null || true
cp nginx_log_collector.py "$AGENT_DIR/" 2>/dev/null || true
cp requirements.txt "$AGENT_DIR/"

# Update VERSION file
echo "$NEW_VERSION" > "$AGENT_DIR/VERSION"

# Restore config
if [[ -f "$CONFIG_BACKUP" ]]; then
    log "Restoring configuration..."
    cp "$CONFIG_BACKUP" "$AGENT_DIR/agent_config.json"
    rm "$CONFIG_BACKUP"
fi

# Update Python dependencies
log "Updating Python dependencies..."
cd "$AGENT_DIR"

# Use virtual environment if it exists
if [[ -d "venv" ]]; then
    source venv/bin/activate
fi

python3 -m pip install -r requirements.txt --upgrade --quiet 2>/dev/null || \
    python3 -m pip install -r requirements.txt --upgrade --quiet --break-system-packages 2>/dev/null || \
    warning "Could not update dependencies automatically"

# Reload systemd if service file changed
systemctl daemon-reload

# Start the service
log "Starting agent service..."
systemctl start "$SERVICE_NAME"

# Wait a moment and check status
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Agent restarted successfully!"
    echo
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✓ Upgrade completed successfully!${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
    echo
    log "Previous version: $CURRENT_VERSION"
    log "Current version:  $NEW_VERSION"
    echo
    log "Backup location: $BACKUP_DIR"
    log "View logs: journalctl -u $SERVICE_NAME -f"
    echo
else
    error "Failed to start agent. Rolling back..."
    systemctl stop "$SERVICE_NAME"
    rm -rf "$AGENT_DIR"
    mv "$BACKUP_DIR" "$AGENT_DIR"
    systemctl start "$SERVICE_NAME"
    error "Upgrade failed. Restored from backup."
fi

# Cleanup
cd /
rm -rf "$TEMP_DIR"

echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo
