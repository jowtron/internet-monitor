#!/usr/bin/env python3
"""
NAS Internet Monitor

Monitors internet connectivity by pinging multiple targets, logs results to CSV,
and sends heartbeats to a VPS monitor over Tailscale. When outages are detected,
accumulated data is sent to the VPS upon reconnection.
"""

import csv
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

import requests
import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Configuration for the NAS monitor."""
    ping_targets: list = field(default_factory=lambda: ['1.1.1.1', '8.8.8.8'])
    ping_interval_seconds: int = 30
    ping_timeout_seconds: int = 5
    ping_count: int = 3
    heartbeat_interval_seconds: int = 60
    vps_url: str = 'http://100.64.0.1:5000'  # Tailscale IP
    log_directory: str = './logs'
    ntfy_topic: str = ''  # Optional direct ntfy notifications from NAS

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
            'PING_TARGETS': ('ping_targets', lambda x: x.split(',')),
            'PING_INTERVAL': ('ping_interval_seconds', int),
            'PING_TIMEOUT': ('ping_timeout_seconds', int),
            'HEARTBEAT_INTERVAL': ('heartbeat_interval_seconds', int),
            'VPS_URL': ('vps_url', str),
            'LOG_DIRECTORY': ('log_directory', str),
            'NTFY_TOPIC': ('ntfy_topic', str),
        }

        for env_var, (attr, converter) in env_mappings.items():
            value = os.environ.get(env_var)
            if value:
                try:
                    setattr(config, attr, converter(value))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid value for {env_var}: {e}")

        return config


@dataclass
class PingResult:
    """Result of a ping operation."""
    timestamp: float
    datetime_str: str
    status: str  # 'online' or 'outage'
    ping_ms: Optional[float]
    target: str


@dataclass
class OutageEvent:
    """Represents a detected outage."""
    start_time: float
    start_datetime: str
    end_time: Optional[float] = None
    end_datetime: Optional[str] = None
    duration_seconds: Optional[float] = None


class PingMonitor:
    """Handles ping operations to check internet connectivity."""

    def __init__(self, config: Config):
        self.config = config

    def ping(self, target: str) -> tuple[bool, Optional[float]]:
        """
        Ping a target and return (success, latency_ms).
        Uses subprocess for cross-platform compatibility.
        """
        try:
            # Determine ping command based on platform
            if sys.platform == 'darwin':  # macOS
                cmd = ['ping', '-c', str(self.config.ping_count), '-t',
                       str(self.config.ping_timeout_seconds), target]
            elif sys.platform == 'win32':
                cmd = ['ping', '-n', str(self.config.ping_count), '-w',
                       str(self.config.ping_timeout_seconds * 1000), target]
            else:  # Linux
                cmd = ['ping', '-c', str(self.config.ping_count), '-W',
                       str(self.config.ping_timeout_seconds), target]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.ping_timeout_seconds * self.config.ping_count + 5
            )

            if result.returncode == 0:
                # Parse average latency from output
                latency = self._parse_ping_latency(result.stdout)
                return True, latency
            return False, None

        except subprocess.TimeoutExpired:
            logger.debug(f"Ping to {target} timed out")
            return False, None
        except Exception as e:
            logger.error(f"Ping error for {target}: {e}")
            return False, None

    def _parse_ping_latency(self, output: str) -> Optional[float]:
        """Extract average latency from ping output."""
        try:
            # macOS/Linux format: "round-trip min/avg/max/stddev = 1.234/5.678/9.012/1.234 ms"
            # or "rtt min/avg/max/mdev = 1.234/5.678/9.012/1.234 ms"
            for line in output.split('\n'):
                if 'avg' in line.lower() or 'average' in line.lower():
                    # Find the numbers part
                    if '=' in line:
                        numbers_part = line.split('=')[1].strip()
                        # Split by '/' and get the average (second value)
                        parts = numbers_part.split('/')
                        if len(parts) >= 2:
                            return float(parts[1])
            return None
        except (ValueError, IndexError) as e:
            logger.debug(f"Could not parse ping latency: {e}")
            return None

    def check_connectivity(self) -> PingResult:
        """
        Check internet connectivity by pinging all targets.
        Returns online if ANY target responds.
        """
        timestamp = time.time()
        datetime_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        best_latency = None
        any_success = False
        successful_target = None

        for target in self.config.ping_targets:
            success, latency = self.ping(target)
            if success:
                any_success = True
                successful_target = target
                if latency is not None:
                    if best_latency is None or latency < best_latency:
                        best_latency = latency
                break  # Stop on first success for efficiency

        return PingResult(
            timestamp=timestamp,
            datetime_str=datetime_str,
            status='online' if any_success else 'outage',
            ping_ms=best_latency,
            target=successful_target or self.config.ping_targets[0]
        )


class CSVLogger:
    """Handles CSV logging of ping results."""

    def __init__(self, config: Config):
        self.config = config
        self.log_dir = Path(config.log_directory)
        self._ensure_log_directory()

    def _ensure_log_directory(self):
        """Create log directory if it doesn't exist."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create log directory: {e}")
            raise

    def _get_log_filename(self) -> Path:
        """Get the log filename for the current date."""
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.log_dir / f"{date_str}.csv"

    def log_result(self, result: PingResult):
        """Log a ping result to the CSV file."""
        log_file = self._get_log_filename()
        file_exists = log_file.exists()

        try:
            with open(log_file, 'a', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(['timestamp', 'datetime', 'status', 'ping_ms', 'target'])
                writer.writerow([
                    result.timestamp,
                    result.datetime_str,
                    result.status,
                    result.ping_ms if result.ping_ms is not None else '',
                    result.target
                ])
        except OSError as e:
            logger.error(f"Failed to write to log file: {e}")
            # Continue operation even if logging fails


class VPSClient:
    """Handles communication with the VPS monitor."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.timeout = 10

    def send_heartbeat(self) -> bool:
        """Send a heartbeat to the VPS."""
        try:
            response = self.session.post(
                f"{self.config.vps_url}/heartbeat",
                json={
                    'timestamp': time.time(),
                    'datetime': datetime.now().isoformat(),
                    'source': 'nas-monitor'
                },
                timeout=10
            )
            return response.status_code == 200
        except requests.RequestException as e:
            logger.debug(f"Failed to send heartbeat: {e}")
            return False

    def send_outage_report(self, outage: OutageEvent) -> bool:
        """Send an outage report to the VPS."""
        try:
            response = self.session.post(
                f"{self.config.vps_url}/outage",
                json={
                    'start_time': outage.start_time,
                    'start_datetime': outage.start_datetime,
                    'end_time': outage.end_time,
                    'end_datetime': outage.end_datetime,
                    'duration_seconds': outage.duration_seconds,
                    'source': 'nas-monitor'
                },
                timeout=10
            )
            return response.status_code == 200
        except requests.RequestException as e:
            logger.warning(f"Failed to send outage report: {e}")
            return False


class InternetMonitor:
    """Main monitor orchestrating all components."""

    def __init__(self, config: Config):
        self.config = config
        self.ping_monitor = PingMonitor(config)
        self.csv_logger = CSVLogger(config)
        self.vps_client = VPSClient(config)

        self.running = False
        self.current_outage: Optional[OutageEvent] = None
        self.pending_outages: Queue[OutageEvent] = Queue()
        self.last_heartbeat_time = 0
        self.last_status = 'online'

        # For graceful shutdown
        self._shutdown_event = threading.Event()

    def _handle_status_change(self, result: PingResult):
        """Handle transitions between online and outage states."""
        if result.status == 'outage' and self.last_status == 'online':
            # Outage started
            self.current_outage = OutageEvent(
                start_time=result.timestamp,
                start_datetime=result.datetime_str
            )
            logger.warning(f"OUTAGE DETECTED at {result.datetime_str}")

        elif result.status == 'online' and self.last_status == 'outage':
            # Outage ended
            if self.current_outage:
                self.current_outage.end_time = result.timestamp
                self.current_outage.end_datetime = result.datetime_str
                self.current_outage.duration_seconds = (
                    result.timestamp - self.current_outage.start_time
                )
                duration_min = self.current_outage.duration_seconds / 60
                logger.info(
                    f"OUTAGE ENDED at {result.datetime_str} "
                    f"(duration: {duration_min:.1f} minutes)"
                )
                self.pending_outages.put(self.current_outage)
                self.current_outage = None

        self.last_status = result.status

    def _send_pending_outages(self):
        """Send any pending outage reports to the VPS."""
        while not self.pending_outages.empty():
            try:
                outage = self.pending_outages.get_nowait()
                if self.vps_client.send_outage_report(outage):
                    logger.info("Outage report sent to VPS")
                else:
                    # Re-queue if failed
                    self.pending_outages.put(outage)
                    break
            except Empty:
                break

    def _maybe_send_heartbeat(self):
        """Send heartbeat if enough time has passed and we're online."""
        current_time = time.time()
        if (self.last_status == 'online' and
            current_time - self.last_heartbeat_time >= self.config.heartbeat_interval_seconds):
            if self.vps_client.send_heartbeat():
                logger.debug("Heartbeat sent to VPS")
                self.last_heartbeat_time = current_time
                # Also try to send pending outages when we have connectivity
                self._send_pending_outages()
            else:
                logger.debug("Failed to send heartbeat (VPS may be unreachable)")

    def run(self):
        """Main monitoring loop."""
        self.running = True
        logger.info("NAS Internet Monitor started")
        logger.info(f"Ping targets: {self.config.ping_targets}")
        logger.info(f"Ping interval: {self.config.ping_interval_seconds}s")
        logger.info(f"Heartbeat interval: {self.config.heartbeat_interval_seconds}s")
        logger.info(f"VPS URL: {self.config.vps_url}")
        logger.info(f"Log directory: {self.config.log_directory}")

        # Send initial heartbeat
        self._maybe_send_heartbeat()

        while self.running and not self._shutdown_event.is_set():
            try:
                # Check connectivity
                result = self.ping_monitor.check_connectivity()

                # Log to CSV
                self.csv_logger.log_result(result)

                # Handle status changes
                self._handle_status_change(result)

                # Log current status
                if result.status == 'online':
                    logger.info(
                        f"Status: {result.status} | "
                        f"Ping: {result.ping_ms:.1f}ms" if result.ping_ms else
                        f"Status: {result.status}"
                    )
                else:
                    logger.warning(f"Status: {result.status}")

                # Send heartbeat if appropriate
                self._maybe_send_heartbeat()

                # Wait for next check
                self._shutdown_event.wait(self.config.ping_interval_seconds)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                self._shutdown_event.wait(5)  # Brief pause before retry

        logger.info("NAS Internet Monitor stopped")

    def stop(self):
        """Stop the monitor gracefully."""
        self.running = False
        self._shutdown_event.set()


def main():
    """Main entry point."""
    # Load configuration
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')
    config = Config.load(config_path)

    # Create and run monitor
    monitor = InternetMonitor(config)

    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        monitor.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        monitor.stop()


if __name__ == '__main__':
    main()
