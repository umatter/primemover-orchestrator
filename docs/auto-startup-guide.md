# PrimeMover Auto-Startup Guide

This guide explains how to configure PrimeMover for automatic startup after server reboot, including memory monitoring and health checks.

## Overview

The auto-startup system ensures all PrimeMover components start automatically:

1. **VNC Server** - Required for Multilogin GUI
2. **Docker Containers** - API, MySQL, Redis, monitoring stack
3. **Multilogin Agent & Desktop** - Browser fingerprint management
4. **Lambda Service** - Local video selection
5. **Runner** - Browser automation
6. **Health Check** - Verifies everything works

## Quick Start

### Install Systemd Services

```bash
sudo ./scripts/install-systemd-services.sh
```

This installs:
- `primemover-lambda.service` - Local Lambda server
- `primemover-health-check.service` - Boot-time health verification
- `multilogin-agent.service` - Multilogin agent (manual enable)
- `multilogin-desktop.service` - Multilogin desktop (manual enable)

### Manual Startup

To start all components without systemd:

```bash
./scripts/full-startup.sh
```

## Multilogin Auto-Login Challenge

**The main challenge**: Multilogin X requires manual GUI login on first use after reboot.

### Current Workarounds

1. **API Token Authentication** (Partial)
   - The `multilogin-login.py` script can authenticate via API
   - However, the desktop app still needs to be logged in via VNC initially
   - After first login, the session persists until logout/reboot

2. **VNC Auto-Start**
   - VNC server starts automatically via systemd
   - Connect via VNC Viewer to login to Multilogin

3. **Agent Auto-Start** (After Login)
   - Once logged in via VNC, enable auto-start:
   ```bash
   sudo systemctl enable multilogin-agent
   sudo systemctl enable multilogin-desktop
   ```

### Recommended Workflow After Reboot

1. Server reboots
2. VNC, Docker, Lambda start automatically
3. Connect via VNC: `your-server-ip:5901`
4. Open Multilogin X and log in
5. Run verification: `orchestrator e2e`

## Memory Monitoring

Monitor browser/Multilogin memory to prevent OOM issues:

```bash
# One-time check
./scripts/monitor-memory.sh

# Continuous monitoring (every 30s)
./scripts/monitor-memory.sh --watch

# JSON output for scripts
./scripts/monitor-memory.sh --json
```

### Memory Thresholds

- **Warning**: Browser memory > 2GB
- **Critical**: Available system memory < 1GB

### Preventing OOM During Long Sessions

The OOM killer terminated VNC during a 50-video session. To prevent this:

1. **Split long sessions** into batches of 15-20 videos
2. **Restart browser** between batches
3. **Monitor memory** during tests with `--watch`
4. **Set resource limits** in systemd services

## Systemd Services

### View Status

```bash
systemctl status vncserver@1
systemctl status primemover-lambda
systemctl status primemover-health-check
```

### View Logs

```bash
journalctl -u primemover-lambda -f
journalctl -u primemover-health-check
```

### Manual Control

```bash
# Start
sudo systemctl start primemover-lambda

# Stop
sudo systemctl stop primemover-lambda

# Restart
sudo systemctl restart vncserver@1
```

## Health Check

The boot health check runs automatically after reboot:

```bash
# Manual run with e2e test
./scripts/boot-health-check.sh

# Quick check (no e2e)
./scripts/boot-health-check.sh --quick

# With custom wait time
./scripts/boot-health-check.sh --wait 120
```

### What It Checks

- VNC Server
- Docker containers
- Monitoring stack (Grafana, Loki, Prometheus)
- PrimeMover API
- Lambda Service
- Runner process
- Multilogin Agent & Desktop
- Multilogin Launcher API
- System resources (memory, disk)
- E2E test (optional)

## File Locations

| File | Purpose |
|------|---------|
| `scripts/full-startup.sh` | Start all components |
| `scripts/monitor-memory.sh` | Memory monitoring |
| `scripts/boot-health-check.sh` | Boot verification |
| `scripts/multilogin-login.py` | API authentication |
| `scripts/install-systemd-services.sh` | Install services |
| `infrastructure/systemd/*.service` | Systemd unit files |
| `log/memory-monitor.log` | Memory history |
| `log/boot-health-check.log` | Boot check results |

## Troubleshooting

### Multilogin Not Responding

```bash
# Check if processes are running
pgrep -a agent.bin
pgrep -a desktop.bin

# Check launcher API
curl -k https://launcher.mlx.yt:45001/api/v2

# Restart via VNC
# Connect to VNC and restart Multilogin X app
```

### OOM Killer Activated

```bash
# Check if VNC was killed
systemctl status vncserver@1

# Check dmesg for OOM
dmesg | grep -i oom

# Restart VNC
sudo systemctl restart vncserver@1
```

### Lambda Service Not Starting

```bash
# Check logs
journalctl -u primemover-lambda

# Manual start
cd /home/primemover/primemover-orchestrator
source venv/bin/activate
python -m orchestrator.lambda_service.server --port 5001
```

## References

- [Multilogin X Auto-Launch Guide](https://multilogin.com/help/auto-launching-the-app)
- [Multilogin X API Documentation](https://documenter.getpostman.com/view/28533318/2s946h9Cv9)
- [Multilogin Python Scripts](https://multilogin.com/help/en_US/custom-api-scripts-with-python/log-in-automatically)
