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

## Deployment Notes

Both services use systemd with:
- `After=tailscaled.service` dependency
- Security hardening (NoNewPrivileges, ProtectSystem)
- Auto-restart on failure
- Journal logging (view with `journalctl -u vps-monitor -f`)

## Common Modifications

- **Add ping targets**: Edit `ping_targets` list in nas-monitor/config.yaml
- **Change heartbeat threshold**: Set `heartbeat_timeout_seconds` in vps-monitor/config.yaml (default 180s)
- **Custom ntfy server**: Set `ntfy_server_url` in vps-monitor/config.yaml
