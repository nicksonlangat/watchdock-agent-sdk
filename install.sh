#!/bin/bash

# WatchDock Agent - Automated Installation Script
# Usage: sudo ./install.sh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
AGENT_DIR="/opt/watchdock-agent"
SERVICE_NAME="watchdock-agent"
CONFIG_FILE="agent_config.json"

# Logging function
log() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
    fi
}

# Detect operating system
detect_os() {
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        OS=$NAME
        VERSION=$VERSION_ID
    else
        error "Cannot detect operating system"
    fi

    log "Detected OS: $OS $VERSION"
}

# Check for Python and install minimal dependencies if needed
install_dependencies() {
    log "Checking system dependencies..."

    # Check if python3 is available
    if ! command -v python3 &> /dev/null; then
        error "Python 3 is required but not found. Please install Python 3 before running this installer."
    else
        log "Using existing Python 3: $(python3 --version)"
    fi

    # Check if pip is available, install if needed
    if ! python3 -m pip --version &> /dev/null; then
        warning "pip not found. Installing pip..."

        if [[ "$OS" == *"Ubuntu"* ]] || [[ "$OS" == *"Debian"* ]]; then
            apt-get update -qq
            apt-get install -y python3-pip
        elif [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]] || [[ "$OS" == *"Rocky"* ]]; then
            yum update -y -q
            yum install -y python3-pip
        elif [[ "$OS" == *"Amazon Linux"* ]]; then
            yum update -y -q
            yum install -y python3-pip
        else
            error "pip not found and unknown OS. Please install python3-pip manually"
        fi
    fi

    # Install basic utilities if missing
    log "Checking for basic utilities..."

    # Check if curl/wget already exist
    if command -v curl &> /dev/null && command -v wget &> /dev/null; then
        log "curl and wget already installed"
        return 0
    fi

    log "Installing curl and wget..."
    if [[ "$OS" == *"Ubuntu"* ]] || [[ "$OS" == *"Debian"* ]]; then
        log "Updating package lists (this may take a moment)..."
        apt-get update -qq || warning "Package update had issues, continuing anyway..."
        apt-get install -y curl wget || warning "Could not install curl/wget"
    elif [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]] || [[ "$OS" == *"Rocky"* ]] || [[ "$OS" == *"Amazon Linux"* ]]; then
        yum install -y curl wget || warning "Could not install curl/wget"
    fi

    log "Utilities check complete"
}

# Create agent directory and copy files
setup_agent_files() {
    log "Setting up agent files..."

    # Create agent directory
    mkdir -p "$AGENT_DIR"

    # Copy agent files
    cp agent.py "$AGENT_DIR/"
    cp config.py "$AGENT_DIR/"
    cp log_parser.py "$AGENT_DIR/"
    cp docker_monitor.py "$AGENT_DIR/"
    cp container_log_collector.py "$AGENT_DIR/"
    cp nginx_log_collector.py "$AGENT_DIR/"
    cp requirements.txt "$AGENT_DIR/"

    # Make agent executable
    chmod +x "$AGENT_DIR/agent.py"

    log "Agent files installed to $AGENT_DIR"
}

# Install Python dependencies
install_python_deps() {
    log "Installing Python dependencies..."

    cd "$AGENT_DIR"

    # Helper function to create venv
    create_venv() {
        log "Creating Python virtual environment..."

        # Check if python3-venv is available, install if not
        if ! python3 -m venv --help &> /dev/null; then
            log "Installing python3-venv package..."
            if [[ "$OS" == *"Ubuntu"* ]] || [[ "$OS" == *"Debian"* ]]; then
                apt-get install -y python3-venv || {
                    error "Failed to install python3-venv. Please run: apt install python3-venv"
                }
            elif [[ "$OS" == *"CentOS"* ]] || [[ "$OS" == *"Red Hat"* ]] || [[ "$OS" == *"Rocky"* ]] || [[ "$OS" == *"Amazon Linux"* ]]; then
                yum install -y python3-virtualenv || warning "Could not install python3-virtualenv"
            fi
        fi

        # Create venv
        python3 -m venv venv || error "Failed to create virtual environment"
        source venv/bin/activate
        python3 -m pip install -r requirements.txt
        PYTHON_EXEC="$AGENT_DIR/venv/bin/python3"
    }

    # Create virtual environment for externally managed Python environments (Ubuntu 24.04+)
    if python3 -m pip install --help 2>/dev/null | grep -q "externally-managed-environment"; then
        log "Detected externally managed Python environment"
        create_venv
    else
        # Try system-wide installation with fallback
        python3 -m pip install -r requirements.txt --break-system-packages 2>/dev/null || {
            log "System-wide installation failed, using virtual environment..."
            create_venv
        }
    fi

    log "Python dependencies installed"
}

# Check and setup configuration
setup_config() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        error "Configuration file '$CONFIG_FILE' not found in current directory!"
    fi

    # Validate config file
    log "Validating configuration file..."
    python3 -c "import json; json.load(open('$CONFIG_FILE'))" 2>/dev/null || error "Invalid JSON in $CONFIG_FILE"

    # Create agent directory if it doesn't exist
    mkdir -p "$AGENT_DIR"

    # Copy config to agent directory
    cp "$CONFIG_FILE" "$AGENT_DIR/"

    log "Configuration file validated and copied"
}

# Setup log directories and permissions
setup_log_directories() {
    log "Setting up log directories and permissions..."

    # Common log directories
    mkdir -p /var/log/nginx /var/log/gunicorn /var/log/watchdock-agent

    # Set permissions for common web server users
    if id "nginx" &>/dev/null; then
        chown nginx:nginx /var/log/nginx
    fi

    if id "www-data" &>/dev/null; then
        chown www-data:www-data /var/log/nginx /var/log/gunicorn
    fi

    # Create agent log file
    touch /var/log/watchdock-agent/agent.log
    chmod 644 /var/log/watchdock-agent/agent.log
}

# Create systemd service
create_service() {
    log "Creating systemd service..."

    # Use virtual environment Python if it exists
    PYTHON_PATH="${PYTHON_EXEC:-/usr/bin/python3}"

    cat > "/etc/systemd/system/$SERVICE_NAME.service" << EOF
[Unit]
Description=WatchDock Agent
Documentation=https://watchdock.cc
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$AGENT_DIR
ExecStart=$PYTHON_PATH $AGENT_DIR/agent.py
ExecReload=/bin/kill -HUP \$MAINPID
KillMode=process
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Security settings
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$AGENT_DIR /var/log

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd and enable service
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"

    log "Systemd service created and enabled"
}

# Setup log rotation
setup_logrotate() {
    log "Setting up log rotation..."

    cat > "/etc/logrotate.d/watchdock-agent" << EOF
/var/log/watchdock-agent/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    create 644 root root
    postrotate
        systemctl reload $SERVICE_NAME
    endscript
}
EOF

    log "Log rotation configured"
}

# Test agent configuration
test_agent() {
    log "Testing agent configuration..."

    cd "$AGENT_DIR"
    PYTHON_PATH="${PYTHON_EXEC:-python3}"
    timeout 10s "$PYTHON_PATH" agent.py --test-config 2>/dev/null || {
        warning "Agent test failed or timed out. Check your configuration."
        return 1
    }

    log "Agent configuration test passed"
}

# Start the service
start_service() {
    log "Starting WatchDock Agent..."

    systemctl start "$SERVICE_NAME"
    sleep 2

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log "Agent started successfully!"
        echo
        echo -e "${GREEN}✓ Installation completed successfully!${NC}"
        echo
        echo "Service status: $(systemctl is-active $SERVICE_NAME)"
        echo "To view logs: journalctl -u $SERVICE_NAME -f"
        echo "To restart: sudo systemctl restart $SERVICE_NAME"
        echo "To stop: sudo systemctl stop $SERVICE_NAME"
    else
        error "Failed to start agent. Check logs: journalctl -u $SERVICE_NAME"
    fi
}

# Show usage information
show_usage() {
    echo "WatchDock Agent Installer"
    echo
    echo "Usage: sudo ./install.sh [options]"
    echo
    echo "Options:"
    echo "  --uninstall    Remove the agent and all related files"
    echo "  --restart      Restart the agent service"
    echo "  --status       Show agent service status"
    echo "  --logs         Show agent logs"
    echo "  --help         Show this help message"
    echo
    echo "Before running, ensure you have a valid 'agent_config.json' file in the current directory."
}

# Uninstall function
uninstall_agent() {
    log "Uninstalling WatchDock Agent..."

    # Stop and disable service
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true

    # Remove service file
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload

    # Remove agent directory
    rm -rf "$AGENT_DIR"

    # Remove log rotation
    rm -f "/etc/logrotate.d/watchdock-agent"

    log "Agent uninstalled successfully"
}

# Main installation process
main() {
    echo -e "${BLUE}================================${NC}"
    echo -e "${BLUE}      WatchDock Agent           ${NC}"
    echo -e "${BLUE}     Automated Installer        ${NC}"
    echo -e "${BLUE}================================${NC}"
    echo

    case "${1:-install}" in
        --uninstall)
            check_root
            uninstall_agent
            ;;
        --restart)
            check_root
            systemctl restart "$SERVICE_NAME"
            log "Agent restarted"
            ;;
        --status)
            systemctl status "$SERVICE_NAME"
            ;;
        --logs)
            journalctl -u "$SERVICE_NAME" -f
            ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        install|"")
            check_root
            detect_os
            install_dependencies
            setup_config
            setup_agent_files
            install_python_deps
            setup_log_directories
            create_service
            setup_logrotate
            test_agent
            start_service
            ;;
        *)
            error "Unknown option: $1. Use --help for usage information."
            ;;
    esac
}

# Run main function
main "$@"