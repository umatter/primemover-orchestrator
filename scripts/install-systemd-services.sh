#!/bin/bash
#
# Install PrimeMover systemd services
#
# This script installs systemd service files for automatic startup
# of all PrimeMover components after reboot.
#
# Usage:
#   sudo ./install-systemd-services.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SYSTEMD_DIR="$PROJECT_DIR/infrastructure/systemd"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}This script must be run as root (use sudo)${NC}"
    exit 1
fi

echo -e "${CYAN}Installing PrimeMover systemd services...${NC}"
echo ""

# Copy service files
for service in primemover-lambda multilogin-agent multilogin-desktop primemover-health-check; do
    src="$SYSTEMD_DIR/${service}.service"
    if [[ -f "$src" ]]; then
        cp "$src" /etc/systemd/system/
        echo -e "${GREEN}✓${NC} Installed $service.service"
    else
        echo -e "${YELLOW}⚠${NC} $src not found, skipping"
    fi
done

# Reload systemd
systemctl daemon-reload
echo -e "${GREEN}✓${NC} Systemd daemon reloaded"

# Enable services
echo ""
echo -e "${CYAN}Enabling services...${NC}"

# VNC should already be enabled, but make sure
systemctl enable vncserver@1 2>/dev/null || true
echo -e "${GREEN}✓${NC} vncserver@1 enabled"

# Enable our services
for service in primemover-lambda primemover-health-check; do
    systemctl enable "$service" 2>/dev/null || true
    echo -e "${GREEN}✓${NC} $service enabled"
done

# Multilogin services are optional - they may need manual login
echo ""
echo -e "${YELLOW}Note: Multilogin services not auto-enabled.${NC}"
echo "Multilogin X requires manual login via VNC on first use."
echo "After logging in once, you can enable auto-start with:"
echo "  sudo systemctl enable multilogin-agent"
echo "  sudo systemctl enable multilogin-desktop"
echo ""

echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "Services installed:"
echo "  - vncserver@1 (already existed)"
echo "  - primemover-lambda"
echo "  - primemover-health-check"
echo "  - multilogin-agent (manual enable)"
echo "  - multilogin-desktop (manual enable)"
echo ""
echo "To start all services now:"
echo "  $SCRIPT_DIR/full-startup.sh"
echo ""
echo "To test the setup after reboot:"
echo "  sudo reboot"
echo "  # After reboot, check:"
echo "  systemctl status primemover-health-check"
echo "  journalctl -u primemover-health-check"
echo ""
