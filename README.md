# Distributed Internet Outage Monitor

A two-component system for monitoring home internet connectivity and receiving notifications about outages.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              ARCHITECTURE                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   HOME NETWORK                           VPS (CLOUD)                        │
│   ┌──────────────────┐                   ┌──────────────────┐               │
│   │   NAS Monitor    │                   │   VPS Monitor    │               │
│   │                  │   Tailscale VPN   │                  │               │
│   │  ┌────────────┐  │ ─────────────────►│  ┌────────────┐  │               │
│   │  │ Ping Check │  │    Heartbeat      │  │  Receiver  │  │               │
│   │  │ 1.1.1.1    │  │    every 60s      │  │  Flask     │  │               │
│   │  │ 8.8.8.8    │  │ ─────────────────►│  └────────────┘  │               │
│   │  └────────────┘  │    Outage Data    │         │        │               │
│   │         │        │                   │         ▼        │               │
│   │         ▼        │                   │  ┌────────────┐  │               │
│   │  ┌────────────┐  │                   │  │   ntfy     │──┼──► Phone      │
│   │  │ CSV Logger │  │                   │  │ Notifier   │  │   Alerts     │
│   │  │ YYYY-MM-DD │  │                   │  └────────────┘  │               │
│   │  └────────────┘  │                   │                  │               │
│   └──────────────────┘                   └──────────────────┘               │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **NAS Monitor** pings DNS servers (1.1.1.1, 8.8.8.8) every 30 seconds
2. Results are logged to daily CSV files
3. Heartbeats are sent to VPS every 60 seconds over Tailscale
4. **VPS Monitor** tracks heartbeats; if none received for 3 minutes, sends "DOWN" notification via ntfy
5. When heartbeats resume, sends "RESTORED" notification with outage duration
6. When NAS detects outage recovery, it sends detailed outage data to VPS

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| [nas-monitor](./nas-monitor/) | Home NAS/Server | Monitors connectivity, logs to CSV, sends heartbeats |
| [vps-monitor](./vps-monitor/) | Cloud VPS | Receives heartbeats, sends ntfy notifications |

## Prerequisites

- Python 3.8+
- Tailscale installed and connected on both machines
- ntfy.sh account (or self-hosted ntfy server)

## Quick Start

### 1. Setup Tailscale on Both Machines

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Note your VPS Tailscale IP
tailscale ip -4
```

### 2. Deploy VPS Monitor

```bash
# On your VPS
cd /opt
sudo git clone <repo> vps-monitor
cd vps-monitor/vps-monitor
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
sudo cp config.yaml.example config.yaml
# Edit config.yaml - set ntfy_topic
sudo cp vps-monitor.service /etc/systemd/system/
sudo systemctl enable --now vps-monitor
```

### 3. Deploy NAS Monitor

```bash
# On your NAS
cd /opt
sudo git clone <repo> nas-monitor
cd nas-monitor/nas-monitor
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
sudo cp config.yaml.example config.yaml
# Edit config.yaml - set vps_url to VPS Tailscale IP
sudo cp nas-monitor.service /etc/systemd/system/
sudo systemctl enable --now nas-monitor
```

### 4. Subscribe to ntfy

Install ntfy app on your phone and subscribe to your topic.

## Notifications

| Notification | When | Priority |
|--------------|------|----------|
| Home Network DOWN | No heartbeat for 3 minutes | High |
| Home Network RESTORED | Heartbeat received after outage | Normal |

## CSV Log Format

```csv
timestamp,datetime,status,ping_ms,target
1703847600.123,2024-12-29 10:00:00,online,12.5,1.1.1.1
1703847630.456,2024-12-29 10:00:30,outage,,1.1.1.1
```

## Why Tailscale?

- **Security**: No public internet exposure; all traffic encrypted
- **Simplicity**: No port forwarding or firewall configuration needed
- **Reliability**: Works even when home IP changes
- **NAT Traversal**: Works behind CGNAT and complex network setups

## Edge Cases Handled

- **VPS Restart**: Grace period prevents false DOWN alerts
- **Partial Network Failure**: Multiple ping targets for redundancy
- **NAS Offline During VPS Restart**: Will detect on next missed heartbeat
- **Time Sync Issues**: Uses monotonic timestamps for duration calculations
- **Disk Full**: CSV logging failures don't crash the monitor
- **Network Flapping**: Outage data queued and sent on stable reconnection
