#!/bin/bash

# WatchDock Agent - Release Builder
# Creates a distributable tarball for customer deployment

set -e

# Configuration
AGENT_VERSION="${1:-1.0.0}"
BUILD_DIR="build"
RELEASE_NAME="watchdock-agent-${AGENT_VERSION}"
RELEASE_DIR="${BUILD_DIR}/${RELEASE_NAME}"
OUTPUT_FILE="${RELEASE_NAME}.tar.gz"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[BUILD]${NC} $1"
}

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# Clean previous builds
log "Cleaning previous builds..."
rm -rf "$BUILD_DIR"
mkdir -p "$RELEASE_DIR"

# Copy agent files
log "Copying agent files..."
cp agent.py "$RELEASE_DIR/"
cp config.py "$RELEASE_DIR/"
cp docker_monitor.py "$RELEASE_DIR/"
cp container_log_collector.py "$RELEASE_DIR/"
cp nginx_log_collector.py "$RELEASE_DIR/"
cp requirements.txt "$RELEASE_DIR/"
cp install.sh "$RELEASE_DIR/"
cp upgrade.sh "$RELEASE_DIR/"
cp quickstart.sh "$RELEASE_DIR/" 2>/dev/null || true

# Make scripts executable
chmod +x "$RELEASE_DIR/install.sh"
chmod +x "$RELEASE_DIR/upgrade.sh"
chmod +x "$RELEASE_DIR/quickstart.sh" 2>/dev/null || true
chmod +x "$RELEASE_DIR/agent.py"

# Copy documentation
log "Copying documentation..."
cp README.md "$RELEASE_DIR/" 2>/dev/null || true
cp INSTALL.md "$RELEASE_DIR/" 2>/dev/null || true
cp CUSTOMER_GUIDE.md "$RELEASE_DIR/" 2>/dev/null || true

# Create example config (without sensitive data)
log "Creating example configuration..."
cp agent_config.json.example "$RELEASE_DIR/"

# Create VERSION file
log "Creating version file..."
echo "$AGENT_VERSION" > "$RELEASE_DIR/VERSION"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RELEASE_DIR/BUILD_DATE"

# Create quick start guide
log "Creating quick start guide..."
cat > "$RELEASE_DIR/QUICKSTART.txt" << 'EOF'
═══════════════════════════════════════════════════════════════
  Platform Observability Agent - Quick Start Guide
═══════════════════════════════════════════════════════════════

STEP 1: Configure the Agent
────────────────────────────────────────────────────────────────
Copy the example config and edit it with your details:

    cp agent_config.json.example agent_config.json
    nano agent_config.json

Required fields:
  • api_endpoint: https://api.watchdock.cc/api
  • api_token: Your organization API token (from dashboard)

The agent will automatically detect and register your server!
No manual log source creation needed.

STEP 2: Install the Agent
────────────────────────────────────────────────────────────────
Run the installer (requires root):

    sudo ./install.sh

This will:
  ✓ Install dependencies
  ✓ Set up systemd service
  ✓ Configure log rotation
  ✓ Start the agent

STEP 3: Verify Installation
────────────────────────────────────────────────────────────────
Check agent status:

    sudo systemctl status watchdock-agent

View logs:

    sudo journalctl -u watchdock-agent -f

MANAGEMENT COMMANDS
────────────────────────────────────────────────────────────────
    sudo ./install.sh --status      # Check status
    sudo ./install.sh --restart     # Restart agent
    sudo ./install.sh --logs        # View logs
    sudo ./install.sh --uninstall   # Remove agent

UPGRADING TO NEW VERSION
────────────────────────────────────────────────────────────────
No need to reconfigure! Just run:

    curl -sSL https://github.com/nicksonlangat/watchdock-agent-sdk/releases/latest/download/upgrade.sh | sudo bash

Or manual upgrade:

    wget https://github.com/nicksonlangat/watchdock-agent-sdk/releases/latest/download/upgrade.sh
    sudo bash upgrade.sh

This will:
  ✓ Backup your current installation
  ✓ Preserve your configuration
  ✓ Update to latest version
  ✓ Restart the service automatically

TROUBLESHOOTING
────────────────────────────────────────────────────────────────
If the agent fails to start:

1. Check logs: sudo journalctl -u watchdock-agent -n 50
2. Verify config: python3 agent.py --test-config
3. Check connectivity: curl -I https://your-api-endpoint.com

For more help, see README.md or visit our documentation.
═══════════════════════════════════════════════════════════════
EOF

# Create SHA256 checksums
log "Creating checksums..."
cd "$RELEASE_DIR"
sha256sum *.py *.sh requirements.txt > SHA256SUMS
cd - > /dev/null

# Create tarball
log "Creating release tarball..."
cd "$BUILD_DIR"
tar -czf "../$OUTPUT_FILE" "$RELEASE_NAME"
cd - > /dev/null

# Calculate final checksum
TARBALL_CHECKSUM=$(sha256sum "$OUTPUT_FILE" | awk '{print $1}')

# Create release info
log "Creating release info..."
cat > "${BUILD_DIR}/release-info.txt" << EOF
Release: ${RELEASE_NAME}
Version: ${AGENT_VERSION}
Built: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Tarball: ${OUTPUT_FILE}
SHA256: ${TARBALL_CHECKSUM}

Installation Instructions:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
wget https://github.com/nicksonlangat/watchdock-agent-sdk/releases/download/agent-v${AGENT_VERSION}/${OUTPUT_FILE}
tar -xzf ${OUTPUT_FILE}
cd ${RELEASE_NAME}
cp agent_config.json.example agent_config.json
# Edit agent_config.json with your API token
sudo ./install.sh
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Files Included:
$(tar -tzf "$OUTPUT_FILE" | sed 's/^/  - /')

EOF

# Show summary
echo
echo "════════════════════════════════════════════════════════════"
echo -e "${GREEN}✓ Release package created successfully!${NC}"
echo "════════════════════════════════════════════════════════════"
echo
info "Version: ${AGENT_VERSION}"
info "Package: ${OUTPUT_FILE}"
info "Size: $(du -h "$OUTPUT_FILE" | awk '{print $1}')"
info "SHA256: ${TARBALL_CHECKSUM}"
echo
info "Release info saved to: ${BUILD_DIR}/release-info.txt"
echo
echo "Next steps:"
echo "  1. Test the package: tar -xzf $OUTPUT_FILE && cd $RELEASE_NAME"
echo "  2. Upload to GitHub Releases"
echo "  3. Update documentation with download links"
echo
echo "════════════════════════════════════════════════════════════"
