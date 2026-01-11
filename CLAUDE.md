# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Distributed internet outage monitoring system with two components communicating over Tailscale VPN.

## Architecture

```
NAS (Home) ──Tailscale──> VPS (Cloud) ──> ntfy (Notifications)
    │                         │
    └── CSV logs              └── outages.log
```

- **nas-monitor**: Pings DNS servers, logs to CSV, sends heartbeats to VPS
- **vps-monitor**: Flask server receiving heartbeats, sends ntfy alerts on outage

## Running Locally

```bash
# NAS Monitor
cd nas-monitor
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python monitor.py

# VPS Monitor
cd vps-monitor
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
NTFY_TOPIC=test-topic ./venv/bin/python monitor.py
```

## Testing Communication

```bash
# Test VPS endpoints
curl http://localhost:5000/health
curl http://localhost:5000/status
curl -X POST http://localhost:5000/heartbeat -H "Content-Type: application/json" -d '{"timestamp": 123}'
```

## Key Configuration

Environment variables override config.yaml:

| NAS | VPS |
|-----|-----|
| VPS_URL | NTFY_TOPIC (required) |
| PING_INTERVAL | HEARTBEAT_TIMEOUT |
| LOG_DIRECTORY | LISTEN_PORT |

## File Structure

```
nas-monitor/
  monitor.py          # Main script: PingMonitor, CSVLogger, VPSClient classes
  config.yaml         # Ping targets, VPS URL, intervals
  nas-monitor.service # Systemd unit

vps-monitor/
  monitor.py          # Main script: Flask app, HeartbeatTracker, NtfyNotifier
  config.yaml         # ntfy settings, timeout thresholds
  vps-monitor.service # Systemd unit
```

## Production Deployment

### NAS (QNAP TS-453be)

- **Tailscale IP**: 100.66.41.139
- **User**: admin
- **Deployment**: Docker container (via Container Station)
- **Docker binary**: `/share/CACHEDEV3_DATA/.qpkg/container-station/usr/bin/docker`
- **Compose files**: `/share/Container/nas-monitor/` (copied by update script)
- **Logs volume**: `/share/Container/nas-monitor/logs/`
- **HTTP port**: 8090 (for speed test triggers)
- **Network mode**: host (required for ping and Tailscale access)

**Important**: Docker is NOT in PATH on QNAP. Always use full path.

**SSH Note**: QNAP has a menu on SSH login. Bypass by passing command directly:
```bash
ssh admin@100.66.41.139 /bin/bash -c "'command here'"
```

**Container env vars** (set in docker-compose.override.yml, gitignored):
- VPS_URL=http://100.89.202.1:5000
- NTFY_TOPIC=jowtron-home-network
- HTTP_PORT=8090

### VPS (Debian)

- **Tailscale IP**: 100.89.202.1
- **User**: root
- **Two separate services**:
  1. `vps-heartbeat` (port 5000) - Internet monitor from this repo
     - Path: `/opt/internet-monitor/vps-monitor/`
     - Service: `vps-heartbeat.service`
     - Has SQLite DB for event history
  2. `vps-monitor` (port 8085) - VPS stats dashboard (separate project)
     - Path: `/root/vps-monitor/`
     - Service: `vps-monitor.service`
     - Proxies to port 5000 for `/monitor/internet/*` routes

### Tailscale Serve Routes

```
https://incrediblepbx.merino-komodo.ts.net
├── /                  → http://127.0.0.1:80
├── /monitor           → http://127.0.0.1:8085 (VPS stats + Home Internet summary)
└── /monitor/internet  → http://127.0.0.1:5000 (detailed internet dashboard)
```

### Dashboard URLs

- **Main dashboard**: https://incrediblepbx.merino-komodo.ts.net/monitor/
- **Internet details**: https://incrediblepbx.merino-komodo.ts.net/monitor/internet
- **Direct VPS stats**: http://100.89.202.1:8085
- **Direct internet API**: http://100.89.202.1:5000

### Updating

**NAS** (from laptop):
```bash
cd nas-monitor && ./update-nas.sh
```

**VPS**:
```bash
ssh root@100.89.202.1 'cd /opt/internet-monitor && git pull && systemctl restart vps-heartbeat'
```

### Checking Logs

```bash
# NAS container logs
ssh admin@100.66.41.139 '/share/CACHEDEV3_DATA/.qpkg/container-station/usr/bin/docker logs nas-monitor -f'

# VPS heartbeat service
ssh root@100.89.202.1 'journalctl -u vps-heartbeat -f'

# VPS stats dashboard
ssh root@100.89.202.1 'journalctl -u vps-monitor -f'
```

## Common Modifications

- **Add ping targets**: Edit `ping_targets` list in nas-monitor/config.yaml
- **Change heartbeat threshold**: Set `heartbeat_timeout_seconds` in vps-monitor/config.yaml (default 180s)
- **Custom ntfy server**: Set `ntfy_server_url` in vps-monitor/config.yaml
