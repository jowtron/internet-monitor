#!/usr/bin/env python3
"""
VPS Internet Monitor

Receives heartbeats from NAS monitor over Tailscale, tracks connectivity status,
and sends ntfy notifications when the home network goes down or recovers.
"""

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml
from flask import Flask, jsonify, request

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Reduce Flask/Werkzeug logging noise
logging.getLogger('werkzeug').setLevel(logging.WARNING)


@dataclass
class Config:
    """Configuration for the VPS monitor."""
    listen_host: str = '0.0.0.0'
    listen_port: int = 5000
    heartbeat_timeout_seconds: int = 180  # 3 minutes
    check_interval_seconds: int = 30
    ntfy_server_url: str = 'https://ntfy.sh'
    ntfy_topic: str = ''
    outage_log_file: str = './outages.log'
    # Grace period after startup before sending DOWN notifications
    startup_grace_seconds: int = 120

    @classmethod
    def load(cls, config_path: str = 'config.yaml') -> 'Config':
        """Load configuration from YAML file and environment variables."""
        config = cls()

        # Load from YAML if exists
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                yaml_config = yaml.safe_load(f) or {}
                for key, value in yaml_config.items():
                    if hasattr(config, key):
                        setattr(config, key, value)

        # Override with environment variables
        env_mappings = {
            'LISTEN_HOST': ('listen_host', str),
            'LISTEN_PORT': ('listen_port', int),
            'HEARTBEAT_TIMEOUT': ('heartbeat_timeout_seconds', int),
            'CHECK_INTERVAL': ('check_interval_seconds', int),
            'NTFY_SERVER_URL': ('ntfy_server_url', str),
            'NTFY_TOPIC': ('ntfy_topic', str),
            'OUTAGE_LOG_FILE': ('outage_log_file', str),
            'STARTUP_GRACE': ('startup_grace_seconds', int),
        }

        for env_var, (attr, converter) in env_mappings.items():
            value = os.environ.get(env_var)
            if value:
                try:
                    setattr(config, attr, converter(value))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid value for {env_var}: {e}")

        return config


class HeartbeatTracker:
    """Tracks heartbeat status from NAS."""

    def __init__(self, config: Config):
        self.config = config
        self.last_heartbeat_time: Optional[float] = None
        self.last_heartbeat_data: Optional[dict] = None
        self.is_online = True
        self.outage_start_time: Optional[float] = None
        self.startup_time = time.time()
        self._lock = threading.Lock()

    def record_heartbeat(self, data: dict):
        """Record a received heartbeat."""
        with self._lock:
            now = time.time()
            was_offline = not self.is_online

            self.last_heartbeat_time = now
            self.last_heartbeat_data = data
            self.is_online = True

            if was_offline and self.outage_start_time:
                # Calculate outage duration
                duration = now - self.outage_start_time
                self.outage_start_time = None
                return {
                    'event': 'restored',
                    'duration_seconds': duration
                }
            return {'event': 'heartbeat'}

    def check_status(self) -> Optional[dict]:
        """Check if we've missed heartbeats (called periodically)."""
        with self._lock:
            now = time.time()

            # Don't check during startup grace period
            if now - self.startup_time < self.config.startup_grace_seconds:
                return None

            # If we've never received a heartbeat, start tracking
            if self.last_heartbeat_time is None:
                if self.is_online:
                    self.is_online = False
                    self.outage_start_time = now
                    return {'event': 'down', 'reason': 'no_heartbeat_received'}
                return None

            # Check if heartbeat is overdue
            time_since_heartbeat = now - self.last_heartbeat_time
            if time_since_heartbeat > self.config.heartbeat_timeout_seconds:
                if self.is_online:
                    self.is_online = False
                    self.outage_start_time = self.last_heartbeat_time
                    return {
                        'event': 'down',
                        'reason': 'heartbeat_timeout',
                        'last_heartbeat_age': time_since_heartbeat
                    }

            return None

    def get_status(self) -> dict:
        """Get current status for API endpoint."""
        with self._lock:
            now = time.time()
            return {
                'is_online': self.is_online,
                'last_heartbeat_time': self.last_heartbeat_time,
                'last_heartbeat_age_seconds': (
                    now - self.last_heartbeat_time
                    if self.last_heartbeat_time else None
                ),
                'outage_start_time': self.outage_start_time,
                'current_outage_duration_seconds': (
                    now - self.outage_start_time
                    if self.outage_start_time else None
                )
            }


class NtfyNotifier:
    """Sends notifications via ntfy."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def send(self, title: str, message: str, priority: str = 'default',
             tags: list = None) -> bool:
        """Send a notification to ntfy."""
        if not self.config.ntfy_topic:
            logger.warning("No ntfy topic configured, skipping notification")
            return False

        try:
            url = f"{self.config.ntfy_server_url}/{self.config.ntfy_topic}"
            headers = {
                'Title': title,
                'Priority': priority,
            }
            if tags:
                headers['Tags'] = ','.join(tags)

            response = self.session.post(
                url,
                data=message.encode('utf-8'),
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Notification sent: {title}")
                return True
            else:
                logger.error(f"Failed to send notification: {response.status_code}")
                return False

        except requests.RequestException as e:
            logger.error(f"Failed to send notification: {e}")
            return False

    def notify_down(self, reason: str = None):
        """Send notification that home network is down."""
        extra = f" ({reason})" if reason else ""
        self.send(
            title="Home Network DOWN",
            message=f"No heartbeat received from NAS monitor{extra}",
            priority='high',
            tags=['warning', 'house']
        )

    def notify_restored(self, duration_seconds: float):
        """Send notification that home network is restored."""
        duration_min = duration_seconds / 60
        if duration_min < 1:
            duration_str = f"{duration_seconds:.0f} seconds"
        elif duration_min < 60:
            duration_str = f"{duration_min:.1f} minutes"
        else:
            hours = duration_min / 60
            duration_str = f"{hours:.1f} hours"

        self.send(
            title="Home Network RESTORED",
            message=f"Connection restored after {duration_str}",
            priority='default',
            tags=['white_check_mark', 'house']
        )


class OutageLogger:
    """Logs outage events to a file."""

    def __init__(self, config: Config):
        self.config = config
        self.log_path = Path(config.outage_log_file)
        self._ensure_log_file()

    def _ensure_log_file(self):
        """Ensure log file directory exists."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_outage(self, outage_data: dict):
        """Log an outage event."""
        try:
            with open(self.log_path, 'a') as f:
                timestamp = datetime.now().isoformat()
                f.write(f"{timestamp} | {outage_data}\n")
        except OSError as e:
            logger.error(f"Failed to log outage: {e}")


class VPSMonitor:
    """Main VPS monitor application."""

    def __init__(self, config: Config):
        self.config = config
        self.tracker = HeartbeatTracker(config)
        self.notifier = NtfyNotifier(config)
        self.outage_logger = OutageLogger(config)

        self.app = Flask(__name__)
        self._setup_routes()

        self.running = False
        self._check_thread: Optional[threading.Thread] = None

    def _setup_routes(self):
        """Setup Flask routes."""

        @self.app.route('/heartbeat', methods=['POST'])
        def heartbeat():
            """Receive heartbeat from NAS."""
            try:
                data = request.get_json() or {}
                data['received_at'] = time.time()
                data['remote_addr'] = request.remote_addr

                result = self.tracker.record_heartbeat(data)

                if result.get('event') == 'restored':
                    duration = result['duration_seconds']
                    logger.info(f"Home network RESTORED after {duration:.1f}s")
                    self.notifier.notify_restored(duration)
                    self.outage_logger.log_outage({
                        'type': 'restored',
                        'duration_seconds': duration
                    })
                else:
                    logger.debug(f"Heartbeat received from {request.remote_addr}")

                return jsonify({'status': 'ok', 'result': result})

            except Exception as e:
                logger.error(f"Error processing heartbeat: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

        @self.app.route('/outage', methods=['POST'])
        def outage_report():
            """Receive outage report from NAS."""
            try:
                data = request.get_json() or {}
                data['received_at'] = time.time()
                data['remote_addr'] = request.remote_addr

                logger.info(f"Outage report received: {data}")
                self.outage_logger.log_outage({
                    'type': 'nas_report',
                    'data': data
                })

                return jsonify({'status': 'ok'})

            except Exception as e:
                logger.error(f"Error processing outage report: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

        @self.app.route('/status', methods=['GET'])
        def status():
            """Get current status."""
            return jsonify(self.tracker.get_status())

        @self.app.route('/health', methods=['GET'])
        def health():
            """Health check endpoint."""
            return jsonify({'status': 'healthy', 'timestamp': time.time()})

    def _check_loop(self):
        """Background loop to check for missed heartbeats."""
        while self.running:
            try:
                result = self.tracker.check_status()
                if result:
                    if result.get('event') == 'down':
                        reason = result.get('reason', 'unknown')
                        logger.warning(f"Home network DOWN: {reason}")
                        self.notifier.notify_down(reason)
                        self.outage_logger.log_outage({
                            'type': 'down_detected',
                            'reason': reason
                        })
            except Exception as e:
                logger.error(f"Error in check loop: {e}")

            time.sleep(self.config.check_interval_seconds)

    def run(self):
        """Run the VPS monitor."""
        self.running = True

        logger.info("VPS Internet Monitor started")
        logger.info(f"Listening on {self.config.listen_host}:{self.config.listen_port}")
        logger.info(f"Heartbeat timeout: {self.config.heartbeat_timeout_seconds}s")
        logger.info(f"ntfy topic: {self.config.ntfy_topic or '(not configured)'}")
        logger.info(f"Startup grace period: {self.config.startup_grace_seconds}s")

        # Start background check thread
        self._check_thread = threading.Thread(target=self._check_loop, daemon=True)
        self._check_thread.start()

        # Run Flask app
        # Using threaded=True for handling concurrent requests
        self.app.run(
            host=self.config.listen_host,
            port=self.config.listen_port,
            threaded=True,
            use_reloader=False  # Disable reloader for production
        )

    def stop(self):
        """Stop the monitor."""
        self.running = False


def main():
    """Main entry point."""
    # Load configuration
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')
    config = Config.load(config_path)

    # Validate required configuration
    if not config.ntfy_topic:
        logger.warning(
            "NTFY_TOPIC not configured. Notifications will be disabled. "
            "Set via config.yaml or NTFY_TOPIC environment variable."
        )

    # Create and run monitor
    monitor = VPSMonitor(config)

    # Setup signal handlers
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        monitor.stop()


if __name__ == '__main__':
    main()
