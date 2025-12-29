#!/usr/bin/env python3
"""
NAS Internet Monitor

Monitors internet connectivity by pinging multiple targets, logs events to CSV,
runs speed tests when triggered, and sends heartbeats to a VPS monitor over Tailscale.
"""

import csv
import io
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

import requests
import yaml

try:
    import speedtest
    SPEEDTEST_AVAILABLE = True
except ImportError:
    SPEEDTEST_AVAILABLE = False

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
    log_retention_days: int = 365  # Keep logs for 1 year
    ntfy_server_url: str = 'https://ntfy.sh'
    ntfy_topic: str = ''  # For speed test notifications
    # Thresholds
    high_latency_threshold_ms: int = 200  # Trigger speed test above this
    slow_speed_threshold_mbps: float = 50.0  # Trigger frequent tests below this
    slow_speed_test_interval_seconds: int = 300  # 5 min when speed is slow
    scheduled_speed_test_interval_seconds: int = 3600  # Hourly speed test
    # HTTP server for manual triggers
    http_port: int = 8080

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
            'LOG_RETENTION_DAYS': ('log_retention_days', int),
            'NTFY_SERVER_URL': ('ntfy_server_url', str),
            'NTFY_TOPIC': ('ntfy_topic', str),
            'HIGH_LATENCY_THRESHOLD': ('high_latency_threshold_ms', int),
            'SLOW_SPEED_THRESHOLD': ('slow_speed_threshold_mbps', float),
            'SCHEDULED_SPEED_TEST_INTERVAL': ('scheduled_speed_test_interval_seconds', int),
            'HTTP_PORT': ('http_port', int),
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
class SpeedTestResult:
    """Result of a speed test."""
    timestamp: float
    datetime_str: str
    speed_mbps: float
    duration_seconds: float
    file_size_bytes: int
    trigger: str  # 'manual', 'post_outage', 'high_latency', 'slow_speed_retest', 'scheduled'
    upload_mbps: Optional[float] = None  # Only for Ookla tests
    test_type: str = 'vps'  # 'vps' or 'ookla'


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
        """Ping a target and return (success, latency_ms)."""
        try:
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
                cmd, capture_output=True, text=True,
                timeout=self.config.ping_timeout_seconds * self.config.ping_count + 5
            )

            if result.returncode == 0:
                latency = self._parse_ping_latency(result.stdout)
                return True, latency
            return False, None

        except subprocess.TimeoutExpired:
            return False, None
        except Exception as e:
            logger.error(f"Ping error for {target}: {e}")
            return False, None

    def _parse_ping_latency(self, output: str) -> Optional[float]:
        """Extract average latency from ping output."""
        try:
            for line in output.split('\n'):
                if 'avg' in line.lower() or 'average' in line.lower():
                    if '=' in line:
                        numbers_part = line.split('=')[1].strip()
                        parts = numbers_part.split('/')
                        if len(parts) >= 2:
                            return float(parts[1])
            return None
        except (ValueError, IndexError):
            return None

    def check_connectivity(self) -> PingResult:
        """Check internet connectivity by pinging targets."""
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
                break

        return PingResult(
            timestamp=timestamp,
            datetime_str=datetime_str,
            status='online' if any_success else 'outage',
            ping_ms=best_latency,
            target=successful_target or self.config.ping_targets[0]
        )


class SpeedTester:
    """Handles speed tests by downloading file from VPS or using Ookla."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def run_test(self, trigger: str = 'manual') -> Optional[SpeedTestResult]:
        """Run a speed test by downloading file from VPS."""
        url = f"{self.config.vps_url}/speedtest"
        timestamp = time.time()
        datetime_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            start_time = time.time()
            response = self.session.get(url, timeout=60, stream=True)

            if response.status_code != 200:
                logger.error(f"Speed test failed: HTTP {response.status_code}")
                return None

            # Download and measure
            total_bytes = 0
            for chunk in response.iter_content(chunk_size=8192):
                total_bytes += len(chunk)

            end_time = time.time()
            duration = end_time - start_time

            if duration > 0:
                speed_mbps = (total_bytes * 8) / (duration * 1_000_000)
            else:
                speed_mbps = 0

            result = SpeedTestResult(
                timestamp=timestamp,
                datetime_str=datetime_str,
                speed_mbps=speed_mbps,
                duration_seconds=duration,
                file_size_bytes=total_bytes,
                trigger=trigger,
                test_type='vps'
            )

            logger.info(f"Speed test (VPS): {speed_mbps:.1f} Mbps ({trigger})")
            return result

        except requests.RequestException as e:
            logger.error(f"Speed test failed: {e}")
            return None

    def run_ookla_test(self, trigger: str = 'manual') -> Optional[SpeedTestResult]:
        """Run a full Ookla speed test (slower but more accurate)."""
        if not SPEEDTEST_AVAILABLE:
            logger.error("speedtest-cli not installed")
            return None

        timestamp = time.time()
        datetime_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            logger.info("Running Ookla speed test (this may take 30-60 seconds)...")
            start_time = time.time()

            st = speedtest.Speedtest()
            st.get_best_server()
            download_speed = st.download() / 1_000_000  # Convert to Mbps
            upload_speed = st.upload() / 1_000_000  # Convert to Mbps

            end_time = time.time()
            duration = end_time - start_time

            result = SpeedTestResult(
                timestamp=timestamp,
                datetime_str=datetime_str,
                speed_mbps=download_speed,
                duration_seconds=duration,
                file_size_bytes=0,
                trigger=trigger,
                upload_mbps=upload_speed,
                test_type='ookla'
            )

            logger.info(f"Speed test (Ookla): {download_speed:.1f} Mbps down, {upload_speed:.1f} Mbps up ({trigger})")
            return result

        except Exception as e:
            logger.error(f"Ookla speed test failed: {e}")
            return None


class EventLogger:
    """Logs events (status changes, speed tests, anomalies) to CSV."""

    def __init__(self, config: Config):
        self.config = config
        self.log_dir = Path(config.log_directory)
        self._ensure_log_directory()
        self._last_cleanup_date: Optional[str] = None

    def _ensure_log_directory(self):
        """Create log directory if it doesn't exist."""
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Failed to create log directory: {e}")
            raise

    def _cleanup_old_logs(self):
        """Delete log files older than retention period."""
        today = datetime.now().strftime('%Y-%m-%d')
        if self._last_cleanup_date == today:
            return

        self._last_cleanup_date = today
        cutoff_date = datetime.now() - timedelta(days=self.config.log_retention_days)

        try:
            for log_file in self.log_dir.glob('*.csv'):
                try:
                    file_date = datetime.strptime(log_file.stem, '%Y-%m-%d')
                    if file_date < cutoff_date:
                        log_file.unlink()
                        logger.info(f"Deleted old log: {log_file.name}")
                except (ValueError, OSError):
                    continue
        except OSError as e:
            logger.warning(f"Error during log cleanup: {e}")

    def _get_log_filename(self) -> Path:
        """Get the log filename for the current date."""
        date_str = datetime.now().strftime('%Y-%m-%d')
        return self.log_dir / f"{date_str}.csv"

    def log_event(self, event_type: str, data: dict):
        """Log an event to the CSV file."""
        self._cleanup_old_logs()
        log_file = self._get_log_filename()
        file_exists = log_file.exists()

        try:
            with open(log_file, 'a', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(['timestamp', 'datetime', 'event_type', 'details'])

                timestamp = data.get('timestamp', time.time())
                datetime_str = data.get('datetime_str', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                details = {k: v for k, v in data.items() if k not in ['timestamp', 'datetime_str']}

                writer.writerow([timestamp, datetime_str, event_type, str(details)])
        except OSError as e:
            logger.error(f"Failed to write to log file: {e}")


class NtfyNotifier:
    """Sends notifications via ntfy."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def send(self, title: str, message: str, priority: str = 'default', tags: list = None) -> bool:
        """Send a notification to ntfy."""
        if not self.config.ntfy_topic:
            return False

        try:
            url = f"{self.config.ntfy_server_url}/{self.config.ntfy_topic}"
            headers = {'Title': title, 'Priority': priority}
            if tags:
                headers['Tags'] = ','.join(tags)

            response = self.session.post(url, data=message.encode('utf-8'), headers=headers, timeout=10)
            return response.status_code == 200
        except requests.RequestException as e:
            logger.error(f"Failed to send notification: {e}")
            return False

    def notify_speed_test(self, result: SpeedTestResult):
        """Send speed test result notification."""
        speed = result.speed_mbps
        if speed < self.config.slow_speed_threshold_mbps:
            title = f"Speed Test: {speed:.1f} Mbps (SLOW)"
            priority = 'high'
            tags = ['warning', 'speedboat']
        else:
            title = f"Speed Test: {speed:.1f} Mbps"
            priority = 'default'
            tags = ['white_check_mark', 'speedboat']

        message = f"Type: {result.test_type}\nTrigger: {result.trigger}\nDownload: {speed:.1f} Mbps"
        if result.upload_mbps is not None:
            message += f"\nUpload: {result.upload_mbps:.1f} Mbps"
        message += f"\nDuration: {result.duration_seconds:.1f}s"
        self.send(title, message, priority, tags)


class VPSClient:
    """Handles communication with the VPS monitor."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()

    def send_heartbeat(self) -> bool:
        """Send a heartbeat to the VPS."""
        try:
            response = self.session.post(
                f"{self.config.vps_url}/heartbeat",
                json={'timestamp': time.time(), 'datetime': datetime.now().isoformat(), 'source': 'nas-monitor'},
                timeout=10
            )
            return response.status_code == 200
        except requests.RequestException:
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
        except requests.RequestException:
            return False


class InternetMonitor:
    """Main monitor orchestrating all components."""

    def __init__(self, config: Config):
        self.config = config
        self.ping_monitor = PingMonitor(config)
        self.speed_tester = SpeedTester(config)
        self.event_logger = EventLogger(config)
        self.notifier = NtfyNotifier(config)
        self.vps_client = VPSClient(config)

        self.running = False
        self.current_outage: Optional[OutageEvent] = None
        self.pending_outages: Queue[OutageEvent] = Queue()
        self.last_heartbeat_time = 0
        self.last_status = 'online'
        self.last_speed_test_time = 0
        self.last_scheduled_test_time = 0
        self.in_slow_speed_mode = False
        self.speed_test_requested = threading.Event()

        self._shutdown_event = threading.Event()

    def request_speed_test(self, use_ookla: bool = False) -> Optional[SpeedTestResult]:
        """Trigger a manual speed test."""
        if use_ookla:
            result = self.speed_tester.run_ookla_test(trigger='manual')
        else:
            result = self.speed_tester.run_test(trigger='manual')

        if result:
            log_data = {
                'timestamp': result.timestamp,
                'datetime_str': result.datetime_str,
                'speed_mbps': result.speed_mbps,
                'trigger': result.trigger,
                'test_type': result.test_type
            }
            if result.upload_mbps is not None:
                log_data['upload_mbps'] = result.upload_mbps
            self.event_logger.log_event('speed_test', log_data)
            self.notifier.notify_speed_test(result)
            self._check_slow_speed(result)
        return result

    def request_full_speed_test(self) -> dict:
        """Run all speed tests (VPS + Ookla) and send combined notification."""
        results = {}

        # Run VPS download test
        logger.info("Running full speed test suite...")
        vps_result = self.speed_tester.run_test(trigger='manual_full')
        if vps_result:
            results['vps_download_mbps'] = vps_result.speed_mbps
            self.event_logger.log_event('speed_test', {
                'timestamp': vps_result.timestamp,
                'datetime_str': vps_result.datetime_str,
                'speed_mbps': vps_result.speed_mbps,
                'trigger': 'manual_full',
                'test_type': 'vps'
            })

        # Run Ookla test (download + upload)
        ookla_result = self.speed_tester.run_ookla_test(trigger='manual_full')
        if ookla_result:
            results['ookla_download_mbps'] = ookla_result.speed_mbps
            results['ookla_upload_mbps'] = ookla_result.upload_mbps
            self.event_logger.log_event('speed_test', {
                'timestamp': ookla_result.timestamp,
                'datetime_str': ookla_result.datetime_str,
                'speed_mbps': ookla_result.speed_mbps,
                'upload_mbps': ookla_result.upload_mbps,
                'trigger': 'manual_full',
                'test_type': 'ookla'
            })

        # Send combined notification
        if results:
            self._notify_full_speed_test(results)

        return results

    def _notify_full_speed_test(self, results: dict):
        """Send a combined notification for full speed test."""
        lines = ["Full Speed Test Results:", ""]

        vps_dl = results.get('vps_download_mbps')
        ookla_dl = results.get('ookla_download_mbps')
        ookla_ul = results.get('ookla_upload_mbps')

        if vps_dl is not None:
            lines.append(f"VPS Download: {vps_dl:.1f} Mbps")
        if ookla_dl is not None:
            lines.append(f"Ookla Download: {ookla_dl:.1f} Mbps")
        if ookla_ul is not None:
            lines.append(f"Ookla Upload: {ookla_ul:.1f} Mbps")

        # Check if any download speed is slow (upload has different expectations)
        threshold = self.config.slow_speed_threshold_mbps
        is_slow = any(
            v is not None and v < threshold
            for v in [vps_dl, ookla_dl]
        )

        if is_slow:
            title = "Full Speed Test: SLOW"
            priority = 'high'
            tags = ['warning', 'speedboat']
        else:
            title = "Full Speed Test: OK"
            priority = 'default'
            tags = ['white_check_mark', 'speedboat']

        self.notifier.send(title, "\n".join(lines), priority, tags)

    def _check_slow_speed(self, result: SpeedTestResult):
        """Check if we should enter slow speed mode."""
        if result.speed_mbps < self.config.slow_speed_threshold_mbps:
            if not self.in_slow_speed_mode:
                logger.warning(f"Entering slow speed mode ({result.speed_mbps:.1f} Mbps < {self.config.slow_speed_threshold_mbps} Mbps)")
                self.in_slow_speed_mode = True
        else:
            if self.in_slow_speed_mode:
                logger.info(f"Exiting slow speed mode ({result.speed_mbps:.1f} Mbps)")
                self.in_slow_speed_mode = False

    def _maybe_run_speed_test(self, trigger: str, only_log_if_slow: bool = False) -> Optional[SpeedTestResult]:
        """Run speed test with triage: if VPS test shows slow, confirm with Ookla."""
        result = self.speed_tester.run_test(trigger=trigger)
        if not result:
            return None

        self.last_speed_test_time = time.time()
        vps_speed = result.speed_mbps
        is_slow = vps_speed < self.config.slow_speed_threshold_mbps

        # If VPS test shows slow speed, run Ookla to confirm
        if is_slow and SPEEDTEST_AVAILABLE:
            logger.info(f"VPS test shows {vps_speed:.1f} Mbps - running Ookla to confirm...")
            ookla_result = self.speed_tester.run_ookla_test(trigger=f"{trigger}_confirm")

            if ookla_result:
                # Use Ookla result as the authoritative measurement
                confirmed_slow = ookla_result.speed_mbps < self.config.slow_speed_threshold_mbps

                # Log both results
                self.event_logger.log_event('speed_test', {
                    'timestamp': result.timestamp,
                    'datetime_str': result.datetime_str,
                    'vps_speed_mbps': vps_speed,
                    'ookla_speed_mbps': ookla_result.speed_mbps,
                    'upload_mbps': ookla_result.upload_mbps,
                    'trigger': trigger,
                    'confirmed_slow': confirmed_slow
                })

                # Notify with combined info
                if confirmed_slow:
                    title = f"Speed Test: {ookla_result.speed_mbps:.1f} Mbps (CONFIRMED SLOW)"
                    priority = 'high'
                    tags = ['warning', 'speedboat']
                else:
                    title = f"Speed Test: {ookla_result.speed_mbps:.1f} Mbps (OK)"
                    priority = 'default'
                    tags = ['white_check_mark', 'speedboat']

                message = f"VPS quick test: {vps_speed:.1f} Mbps\nOokla download: {ookla_result.speed_mbps:.1f} Mbps\nOokla upload: {ookla_result.upload_mbps:.1f} Mbps\nTrigger: {trigger}"
                self.notifier.send(title, message, priority, tags)

                # Use Ookla result for slow speed mode decision
                self._check_slow_speed(ookla_result)
                return ookla_result

        # Normal path: VPS test was fine, or Ookla not available
        if not only_log_if_slow or is_slow:
            self.event_logger.log_event('speed_test', {
                'timestamp': result.timestamp,
                'datetime_str': result.datetime_str,
                'speed_mbps': result.speed_mbps,
                'trigger': result.trigger
            })
            self.notifier.notify_speed_test(result)
        else:
            logger.debug(f"Scheduled speed test: {result.speed_mbps:.1f} Mbps (OK, not logged)")

        self._check_slow_speed(result)
        return result

    def _handle_status_change(self, result: PingResult) -> bool:
        """Handle transitions between online and outage states. Returns True if status changed."""
        status_changed = False

        if result.status == 'outage' and self.last_status == 'online':
            # Outage started
            self.current_outage = OutageEvent(start_time=result.timestamp, start_datetime=result.datetime_str)
            logger.warning(f"OUTAGE DETECTED at {result.datetime_str}")
            self.event_logger.log_event('outage_start', {
                'timestamp': result.timestamp,
                'datetime_str': result.datetime_str
            })
            status_changed = True

        elif result.status == 'online' and self.last_status == 'outage':
            # Outage ended
            if self.current_outage:
                self.current_outage.end_time = result.timestamp
                self.current_outage.end_datetime = result.datetime_str
                self.current_outage.duration_seconds = result.timestamp - self.current_outage.start_time
                duration_min = self.current_outage.duration_seconds / 60
                logger.info(f"OUTAGE ENDED at {result.datetime_str} (duration: {duration_min:.1f} minutes)")
                self.event_logger.log_event('outage_end', {
                    'timestamp': result.timestamp,
                    'datetime_str': result.datetime_str,
                    'duration_seconds': self.current_outage.duration_seconds
                })
                self.pending_outages.put(self.current_outage)
                self.current_outage = None
                # Run speed test after outage
                self._maybe_run_speed_test('post_outage')
            status_changed = True

        self.last_status = result.status
        return status_changed

    def _send_pending_outages(self):
        """Send any pending outage reports to the VPS."""
        while not self.pending_outages.empty():
            try:
                outage = self.pending_outages.get_nowait()
                if self.vps_client.send_outage_report(outage):
                    logger.info("Outage report sent to VPS")
                else:
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
                self._send_pending_outages()

    def run(self):
        """Main monitoring loop."""
        self.running = True
        logger.info("NAS Internet Monitor started")
        logger.info(f"Ping targets: {self.config.ping_targets}")
        logger.info(f"Ping interval: {self.config.ping_interval_seconds}s")
        logger.info(f"VPS URL: {self.config.vps_url}")
        logger.info(f"High latency threshold: {self.config.high_latency_threshold_ms}ms")
        logger.info(f"Slow speed threshold: {self.config.slow_speed_threshold_mbps} Mbps")
        logger.info(f"Scheduled speed test: every {self.config.scheduled_speed_test_interval_seconds}s (log only if slow)")
        logger.info(f"HTTP server port: {self.config.http_port}")

        self._maybe_send_heartbeat()

        while self.running and not self._shutdown_event.is_set():
            try:
                # Check for manual speed test request
                if self.speed_test_requested.is_set():
                    self.request_speed_test()
                    self.speed_test_requested.clear()

                # Check connectivity
                result = self.ping_monitor.check_connectivity()
                status_changed = self._handle_status_change(result)

                # Log high latency
                if result.status == 'online' and result.ping_ms:
                    if result.ping_ms > self.config.high_latency_threshold_ms:
                        logger.warning(f"High latency: {result.ping_ms:.1f}ms")
                        self.event_logger.log_event('high_latency', {
                            'timestamp': result.timestamp,
                            'datetime_str': result.datetime_str,
                            'ping_ms': result.ping_ms
                        })
                        # Trigger speed test on high latency
                        self._maybe_run_speed_test('high_latency')

                # Check if we need frequent speed tests (slow speed mode)
                if self.in_slow_speed_mode:
                    time_since_test = time.time() - self.last_speed_test_time
                    if time_since_test >= self.config.slow_speed_test_interval_seconds:
                        self._maybe_run_speed_test('slow_speed_retest')

                # Scheduled hourly speed test (only log if slow)
                time_since_scheduled = time.time() - self.last_scheduled_test_time
                if time_since_scheduled >= self.config.scheduled_speed_test_interval_seconds:
                    self.last_scheduled_test_time = time.time()
                    self._maybe_run_speed_test('scheduled', only_log_if_slow=True)

                # Log status (only on change or hourly)
                if status_changed:
                    if result.status == 'online':
                        logger.info(f"Status: ONLINE | Ping: {result.ping_ms:.1f}ms" if result.ping_ms else "Status: ONLINE")
                    else:
                        logger.warning("Status: OUTAGE")
                else:
                    # Periodic status log (every ~10 min for visibility)
                    if int(time.time()) % 600 < self.config.ping_interval_seconds:
                        if result.status == 'online' and result.ping_ms:
                            logger.info(f"Status: online | Ping: {result.ping_ms:.1f}ms")

                self._maybe_send_heartbeat()
                self._shutdown_event.wait(self.config.ping_interval_seconds)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                self._shutdown_event.wait(5)

        logger.info("NAS Internet Monitor stopped")

    def stop(self):
        """Stop the monitor gracefully."""
        self.running = False
        self._shutdown_event.set()


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for manual triggers."""
    monitor: InternetMonitor = None

    def log_message(self, format, *args):
        logger.debug(f"HTTP: {args[0]}")

    def do_GET(self):
        if self.path == '/speedtest':
            result = self.monitor.request_speed_test(use_ookla=False)
            if result:
                response = f'{{"speed_mbps": {result.speed_mbps:.1f}, "trigger": "{result.trigger}", "test_type": "{result.test_type}"}}'
                self.send_response(200)
            else:
                response = '{"error": "Speed test failed"}'
                self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.encode())

        elif self.path == '/speedtest/ookla':
            result = self.monitor.request_speed_test(use_ookla=True)
            if result:
                upload = result.upload_mbps if result.upload_mbps else 0
                response = f'{{"download_mbps": {result.speed_mbps:.1f}, "upload_mbps": {upload:.1f}, "test_type": "{result.test_type}"}}'
                self.send_response(200)
            else:
                response = '{"error": "Ookla speed test failed"}'
                self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.encode())

        elif self.path == '/speedtest/full':
            results = self.monitor.request_full_speed_test()
            if results:
                self.send_response(200)
                response = json.dumps(results, indent=2)
            else:
                self.send_response(500)
                response = '{"error": "Full speed test failed"}'
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(response.encode())

        elif self.path == '/status':
            status = {
                'running': self.monitor.running,
                'last_status': self.monitor.last_status,
                'in_slow_speed_mode': self.monitor.in_slow_speed_mode,
                'last_speed_test': self.monitor.last_speed_test_time
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(str(status).replace("'", '"').encode())

        else:
            self.send_response(404)
            self.end_headers()


def run_http_server(monitor: InternetMonitor, port: int):
    """Run the HTTP server in a separate thread."""
    RequestHandler.monitor = monitor
    server = HTTPServer(('0.0.0.0', port), RequestHandler)
    logger.info(f"HTTP server listening on port {port}")
    server.serve_forever()


def main():
    """Main entry point."""
    config_path = os.environ.get('CONFIG_PATH', 'config.yaml')
    config = Config.load(config_path)

    monitor = InternetMonitor(config)

    # Start HTTP server thread
    http_thread = threading.Thread(target=run_http_server, args=(monitor, config.http_port), daemon=True)
    http_thread.start()

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
