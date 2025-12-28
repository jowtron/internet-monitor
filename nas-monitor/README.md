# NAS Internet Monitor

Monitors internet connectivity from your home network, logs results to CSV, and reports status to a VPS monitor over Tailscale.

## Features

- Pings multiple DNS servers (1.1.1.1, 8.8.8.8, 9.9.9.9) for redundancy
- Logs all results to daily CSV files
- Detects outages and tracks duration
- Sends heartbeats to VPS monitor every 60 seconds
- Reports outage details to VPS when connection restores
- Graceful handling of network failures

## Docker Installation (Recommended)

```bash
# Pull the image
docker pull ghcr.io/jowtron/internet-monitor/nas-monitor:latest

# Run with your VPS Tailscale IP
docker run -d \
  --name nas-monitor \
  --network host \
  -e VPS_URL=http://YOUR_VPS_TAILSCALE_IP:5000 \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/jowtron/internet-monitor/nas-monitor:latest
```

Or use docker-compose:

```bash
# Copy and edit docker-compose.yml
curl -O https://raw.githubusercontent.com/jowtron/internet-monitor/main/nas-monitor/docker-compose.yml
# Edit VPS_URL to your VPS Tailscale IP
nano docker-compose.yml
docker compose up -d
```

## Manual Installation

```bash
# Clone or copy files to /opt/nas-monitor
sudo mkdir -p /opt/nas-monitor
sudo cp -r . /opt/nas-monitor/

# Create virtual environment
cd /opt/nas-monitor
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt

# Configure
sudo cp config.yaml.example config.yaml
sudo nano config.yaml  # Edit VPS URL to your Tailscale IP

# Create log directory
sudo mkdir -p /opt/nas-monitor/logs

# Install and start service
sudo cp nas-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable nas-monitor
sudo systemctl start nas-monitor
```

## Configuration

Edit `config.yaml` or use environment variables:

| Setting | Env Variable | Default | Description |
|---------|--------------|---------|-------------|
| ping_targets | PING_TARGETS | 1.1.1.1,8.8.8.8 | Comma-separated ping targets |
| ping_interval_seconds | PING_INTERVAL | 30 | Seconds between pings |
| heartbeat_interval_seconds | HEARTBEAT_INTERVAL | 60 | Seconds between heartbeats |
| vps_url | VPS_URL | (required) | VPS monitor URL (Tailscale IP) |
| log_directory | LOG_DIRECTORY | ./logs | CSV log location |

## CSV Log Format

Daily files named `YYYY-MM-DD.csv`:

```csv
timestamp,datetime,status,ping_ms,target
1703847600.123,2024-12-29 10:00:00,online,12.5,1.1.1.1
1703847630.456,2024-12-29 10:00:30,online,14.2,1.1.1.1
1703847660.789,2024-12-29 10:01:00,outage,,1.1.1.1
```

## Monitoring

```bash
# View status
sudo systemctl status nas-monitor

# View logs
sudo journalctl -u nas-monitor -f

# View today's CSV
cat /opt/nas-monitor/logs/$(date +%Y-%m-%d).csv
```

## Tailscale Setup

Ensure Tailscale is installed and connected on both NAS and VPS:

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# Connect
sudo tailscale up

# Get your Tailscale IP
tailscale ip -4
```

Update `vps_url` in config.yaml to use your VPS's Tailscale IP.
