# VPS Internet Monitor

Receives heartbeats from NAS monitor over Tailscale, detects outages, and sends notifications via ntfy.

## Features

- HTTP endpoint for receiving heartbeats from NAS
- Detects missed heartbeats and triggers DOWN notification
- Sends RESTORED notification with outage duration when heartbeats resume
- Logs all outage events to file
- Startup grace period to avoid false alerts on VPS restart
- Status API for monitoring

## Docker Installation (Recommended)

```bash
# Pull the image
docker pull ghcr.io/jowtron/internet-monitor/vps-monitor:latest

# Run with your ntfy topic
docker run -d \
  --name vps-monitor \
  -p 5000:5000 \
  -e NTFY_TOPIC=your-ntfy-topic \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  ghcr.io/jowtron/internet-monitor/vps-monitor:latest
```

Or use docker-compose:

```bash
curl -O https://raw.githubusercontent.com/jowtron/internet-monitor/main/vps-monitor/docker-compose.yml
# Edit NTFY_TOPIC
nano docker-compose.yml
docker compose up -d
```

## Manual Installation

```bash
# Clone or copy files to /opt/vps-monitor
sudo mkdir -p /opt/vps-monitor
sudo cp -r . /opt/vps-monitor/

# Create virtual environment
cd /opt/vps-monitor
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt

# Configure
sudo cp config.yaml.example config.yaml
sudo nano config.yaml  # Set your ntfy topic

# Install and start service
sudo cp vps-monitor.service /etc/systemd/system/

# IMPORTANT: Edit the service file to set your ntfy topic
sudo nano /etc/systemd/system/vps-monitor.service

sudo systemctl daemon-reload
sudo systemctl enable vps-monitor
sudo systemctl start vps-monitor
```

## Configuration

Edit `config.yaml` or use environment variables:

| Setting | Env Variable | Default | Description |
|---------|--------------|---------|-------------|
| listen_host | LISTEN_HOST | 0.0.0.0 | HTTP listen address |
| listen_port | LISTEN_PORT | 5000 | HTTP listen port |
| heartbeat_timeout_seconds | HEARTBEAT_TIMEOUT | 180 | Seconds before DOWN alert |
| ntfy_server_url | NTFY_SERVER_URL | https://ntfy.sh | ntfy server |
| ntfy_topic | NTFY_TOPIC | (required) | Your ntfy topic |
| startup_grace_seconds | STARTUP_GRACE | 120 | Grace period after startup |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/heartbeat` | POST | Receive heartbeat from NAS |
| `/outage` | POST | Receive outage report from NAS |
| `/status` | GET | Get current monitoring status |
| `/health` | GET | Health check |

### Status Response Example

```json
{
  "is_online": true,
  "last_heartbeat_time": 1703847600.123,
  "last_heartbeat_age_seconds": 45.5,
  "outage_start_time": null,
  "current_outage_duration_seconds": null
}
```

## ntfy Setup

1. Go to [ntfy.sh](https://ntfy.sh) or use your own ntfy server
2. Choose a unique topic name (e.g., `myname-home-network-status`)
3. Subscribe to the topic on your phone (iOS/Android app) or browser
4. Set the topic in config.yaml or the NTFY_TOPIC environment variable

## Monitoring

```bash
# View status
sudo systemctl status vps-monitor

# View logs
sudo journalctl -u vps-monitor -f

# Check status via API
curl http://localhost:5000/status

# View outage log
cat /opt/vps-monitor/outages.log
```

## Tailscale Setup

Ensure Tailscale is installed and connected:

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# Connect
sudo tailscale up

# Get your Tailscale IP (use this in NAS config)
tailscale ip -4
```

## Firewall

The VPS only needs to accept connections from Tailscale network:

```bash
# If using ufw
sudo ufw allow in on tailscale0 to any port 5000
```

No public internet exposure needed - all communication is over Tailscale.
