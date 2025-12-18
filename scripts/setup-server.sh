#!/bin/bash
#
# PrimeMover Server Setup Script
#
# This script sets up a fresh Ubuntu server for PrimeMover development,
# including GUI desktop and VNC for remote access.
#
# Usage (via SSH):
#   curl -fsSL https://raw.githubusercontent.com/umatter/primemover-orchestrator/master/scripts/setup-server.sh | bash
#
# Or if you've already cloned:
#   ./scripts/setup-server.sh
#
# After completion, connect via VNC Viewer to: your-server-ip:5901
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

print_step() {
    echo -e "\n${CYAN}==>${NC} ${1}"
}

print_success() {
    echo -e "${GREEN}✓${NC} ${1}"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} ${1}"
}

print_error() {
    echo -e "${RED}✗${NC} ${1}"
}

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    print_error "Please run as a regular user, not root. The script will use sudo when needed."
    exit 1
fi

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║           PrimeMover Server Setup                             ║"
echo "║                                                               ║"
echo "║  This will install:                                          ║"
echo "║  • XFCE Desktop (lightweight GUI)                            ║"
echo "║  • VNC Server (remote desktop access)                        ║"
echo "║  • Docker, Java 17, Maven                                    ║"
echo "║  • Google Chrome                                             ║"
echo "║  • Multilogin X (browser fingerprint management)             ║"
echo "║                                                               ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

read -p "Continue with server setup? [Y/n] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]?$ ]]; then
    echo "Aborted."
    exit 0
fi

# =============================================================================
# Step 1: System Update
# =============================================================================
print_step "Updating system packages..."
sudo apt update
sudo apt upgrade -y
print_success "System updated"

# =============================================================================
# Step 2: Install Core Prerequisites
# =============================================================================
print_step "Installing core prerequisites..."
sudo apt install -y \
    git \
    curl \
    wget \
    ca-certificates \
    python3 \
    python3-pip \
    python3-venv \
    openjdk-17-jdk \
    maven

print_success "Core prerequisites installed"

# =============================================================================
# Step 2b: Install Docker with Compose Plugin (v2)
# =============================================================================
print_step "Installing Docker with Compose plugin..."

# Check if docker is already installed with compose plugin
if docker compose version &> /dev/null; then
    print_success "Docker Compose v2 already installed: $(docker compose version --short)"
else
    # Add Docker's official GPG key and repository
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    # Add the repository
    CODENAME=$(lsb_release -cs)
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" | \
        sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt update
    sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    print_success "Docker with Compose plugin installed: $(docker compose version --short 2>/dev/null || echo 'installed')"
fi

# Add user to docker group
print_step "Adding user to docker group..."
sudo usermod -aG docker $USER
print_success "User added to docker group (will take effect after re-login)"

# Start Docker service
print_step "Starting Docker service..."
sudo systemctl start docker
sudo systemctl enable docker
print_success "Docker service started and enabled"

# =============================================================================
# Step 3: Node.js (OPTIONAL - GUI now builds with Docker)
# =============================================================================
# Node.js is no longer required since the GUI builds with Docker.
# Uncomment below if you need Node.js for development:
#
# print_step "Installing Node.js LTS..."
# if ! command -v node &> /dev/null; then
#     curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
#     sudo apt install -y nodejs
#     print_success "Node.js installed: $(node --version)"
# else
#     print_success "Node.js already installed: $(node --version)"
# fi
print_step "Skipping Node.js (GUI builds with Docker)"
print_success "Node.js not needed"

# =============================================================================
# Step 4: Install Google Chrome
# =============================================================================
print_step "Installing Google Chrome..."
if ! command -v google-chrome &> /dev/null; then
    wget -qO- https://dl.google.com/linux/linux_signing_key.pub | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
    sudo apt update
    sudo apt install -y google-chrome-stable
    print_success "Google Chrome installed"
else
    print_success "Google Chrome already installed"
fi

# =============================================================================
# Step 5: Install Multilogin X
# =============================================================================
print_step "Installing Multilogin X..."
if ! command -v multiloginx &> /dev/null && ! dpkg -l | grep -q multiloginx; then
    # Download the latest Multilogin X .deb package
    curl -L -f -o /tmp/mlxdeb.deb "https://mlxdists.s3.eu-west-3.amazonaws.com/mlx/latest/multiloginx-amd64.deb" 2>/dev/null
    if [[ -f /tmp/mlxdeb.deb ]]; then
        sudo dpkg -i /tmp/mlxdeb.deb || true
        sudo apt-get install -f -y  # Fix any dependency issues
        rm -f /tmp/mlxdeb.deb
        print_success "Multilogin X installed"
        print_warning "Note: You'll need to log in to Multilogin X via VNC on first use"
    else
        print_warning "Could not download Multilogin X. Install manually via VNC."
    fi
else
    print_success "Multilogin X already installed"
fi

# =============================================================================
# Step 6: Install XFCE Desktop
# =============================================================================
print_step "Installing XFCE desktop environment..."
sudo apt install -y xfce4 xfce4-goodies dbus-x11
print_success "XFCE desktop installed"

# =============================================================================
# Step 7: Install and Configure VNC Server
# =============================================================================
print_step "Installing TigerVNC server..."
sudo apt install -y tigervnc-standalone-server tigervnc-xorg-extension
print_success "TigerVNC installed"

print_step "Configuring VNC..."

# Create VNC directory
mkdir -p ~/.vnc

# Create xstartup script for XFCE
cat > ~/.vnc/xstartup << 'EOF'
#!/bin/bash
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=XFCE
exec startxfce4
EOF
chmod +x ~/.vnc/xstartup

# Set VNC password
echo ""
echo -e "${YELLOW}Set your VNC password (you'll use this to connect via VNC Viewer):${NC}"
vncpasswd

print_success "VNC configured"

# =============================================================================
# Step 8: Create VNC systemd service (auto-start on boot)
# =============================================================================
print_step "Creating VNC systemd service..."

sudo tee /etc/systemd/system/vncserver@.service > /dev/null << EOF
[Unit]
Description=TigerVNC server on display %i
After=syslog.target network.target

[Service]
Type=forking
User=$USER
Group=$USER
WorkingDirectory=$HOME

ExecStartPre=-/usr/bin/vncserver -kill :%i > /dev/null 2>&1
ExecStart=/usr/bin/vncserver :%i -geometry 1920x1080 -depth 24 -localhost no
ExecStop=/usr/bin/vncserver -kill :%i

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vncserver@1
print_success "VNC service created and enabled"

# =============================================================================
# Step 9: Start VNC Server
# =============================================================================
print_step "Starting VNC server..."

# Kill any existing VNC server
vncserver -kill :1 2>/dev/null || true

# Start VNC server
vncserver :1 -geometry 1920x1080 -depth 24 -localhost no

print_success "VNC server started on display :1"

# =============================================================================
# Step 10: Configure Firewall (if UFW is active)
# =============================================================================
if command -v ufw &> /dev/null && sudo ufw status | grep -q "active"; then
    print_step "Configuring firewall..."
    sudo ufw allow 5901/tcp comment 'VNC'
    sudo ufw allow 8090/tcp comment 'PrimeMover API'
    sudo ufw allow 3000/tcp comment 'Grafana'
    print_success "Firewall rules added"
fi

# =============================================================================
# Step 11: Clone Orchestrator (if not already in the repo)
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_DIR=""

if [[ -f "$SCRIPT_DIR/../Makefile" ]]; then
    ORCHESTRATOR_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    print_success "Running from orchestrator directory: $ORCHESTRATOR_DIR"
else
    print_step "Cloning PrimeMover Orchestrator..."
    cd ~
    if [[ ! -d "primemover-orchestrator" ]]; then
        git clone https://github.com/umatter/primemover-orchestrator.git
        print_success "Orchestrator cloned to ~/primemover-orchestrator"
    else
        print_success "Orchestrator already exists at ~/primemover-orchestrator"
    fi
    ORCHESTRATOR_DIR="$HOME/primemover-orchestrator"
fi

# =============================================================================
# Step 12: Run PrimeMover Setup (no GUI required)
# =============================================================================
echo ""
echo -e "${YELLOW}The GUI/VNC setup is complete.${NC}"
echo -e "PrimeMover setup (cloning repos, building, starting) does NOT require a GUI."
echo ""
read -p "Run 'make setup' now to complete PrimeMover installation? [Y/n] " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]?$ ]]; then
    print_step "Running PrimeMover setup..."
    cd "$ORCHESTRATOR_DIR"

    # Need to use newgrp to get docker access in this session
    # Or we can use sudo for the docker commands
    if groups | grep -q docker; then
        make setup
    else
        echo -e "${YELLOW}Note: Docker group not active yet in this session.${NC}"
        echo -e "Running setup with 'sg docker' to access Docker..."
        sg docker -c "make setup"
    fi
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                    Setup Complete!                            ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "VNC Server is running. Connect using VNC Viewer:"
echo ""
echo -e "  ${CYAN}Host:${NC} $(hostname -I | awk '{print $1}'):5901"
echo -e "  ${CYAN}Password:${NC} (the one you just set)"
echo ""
echo -e "VNC is needed for:"
echo -e "  • Logging into Multilogin X (first-time setup)"
echo -e "  • Visually monitoring browser automation"
echo ""
echo -e "Multilogin X:"
echo -e "  Multilogin X has been installed. On first use via VNC:"
echo -e "  1. Launch Multilogin X from the applications menu"
echo -e "  2. Log in with your Multilogin account"
echo -e "  3. For API automation, generate an automation token"
echo ""
echo -e "To manually start/stop VNC server:"
echo -e "  sudo systemctl start vncserver@1"
echo -e "  sudo systemctl stop vncserver@1"
echo ""
echo -e "To check PrimeMover status:"
echo -e "  cd $ORCHESTRATOR_DIR && make status"
echo ""
