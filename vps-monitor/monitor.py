#!/usr/bin/env python3
"""
VPS Internet Monitor

Receives heartbeats from NAS monitor over Tailscale, tracks connectivity status,
stores event history, provides a dashboard, and sends ntfy notifications.
"""

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import yaml
from flask import Flask, jsonify, request, send_file, Response

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Reduce Flask/Werkzeug logging noise
logging.getLogger('werkzeug').setLevel(logging.WARNING)

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Internet Monitor Dashboard</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect fill='%230f0f0f' width='100' height='100' rx='20'/><path d='M25 65 L40 40 L55 55 L75 25' stroke='%234ade80' stroke-width='8' fill='none' stroke-linecap='round' stroke-linejoin='round'/><circle cx='75' cy='25' r='6' fill='%234ade80'/></svg>">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 {
            font-size: 1.5rem;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .status-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        .status-dot.online { background: #22c55e; }
        .status-dot.offline { background: #ef4444; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: #1e293b;
            border-radius: 12px;
            padding: 20px;
            border: 1px solid #334155;
        }
        .card h2 {
            font-size: 0.875rem;
            color: #94a3b8;
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-value {
            font-size: 2rem;
            font-weight: 700;
            color: #f8fafc;
        }
        .stat-unit {
            font-size: 1rem;
            color: #64748b;
            margin-left: 4px;
        }
        .stat-sub {
            font-size: 0.875rem;
            color: #64748b;
            margin-top: 4px;
        }
        .chart-container {
            background: #1e293b;
            border-radius: 12px;
            padding: 20px;
            border: 1px solid #334155;
            margin-bottom: 20px;
        }
        .chart-container h2 {
            font-size: 1rem;
            margin-bottom: 15px;
            color: #f8fafc;
        }
        .chart-wrapper {
            position: relative;
            height: 250px;
        }
        .time-selector {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
        }
        .time-btn {
            padding: 8px 16px;
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            color: #94a3b8;
            cursor: pointer;
            transition: all 0.2s;
        }
        .time-btn:hover { background: #334155; }
        .time-btn.active {
            background: #3b82f6;
            border-color: #3b82f6;
            color: white;
        }
        .events-list {
            max-height: 300px;
            overflow-y: auto;
        }
        .event-item {
            padding: 10px;
            border-bottom: 1px solid #334155;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .event-item:last-child { border-bottom: none; }
        .event-type {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .event-type.speed_test { background: #3b82f6; }
        .event-type.high_latency { background: #f59e0b; }
        .event-type.latency { background: #06b6d4; }
        .event-type.down, .event-type.outage_start { background: #ef4444; }
        .event-type.restored, .event-type.outage_end { background: #22c55e; }
        .event-time { color: #64748b; font-size: 0.875rem; }
        .no-data {
            color: #64748b;
            text-align: center;
            padding: 40px;
        }
        .incidents-table {
            width: 100%;
            border-collapse: collapse;
        }
        .incidents-table th {
            text-align: left;
            padding: 8px 10px;
            border-bottom: 2px solid #334155;
            color: #94a3b8;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .incidents-table td {
            padding: 8px 10px;
            border-bottom: 1px solid #334155;
            font-size: 0.875rem;
        }
        .incidents-table tr:last-child td { border-bottom: none; }
        .badge {
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
            display: inline-block;
        }
        .badge.outage { background: #ef4444; color: white; }
        .badge.slow { background: #f59e0b; color: white; }
        .badge.pass { background: #22c55e; color: white; }
        .badge.fail { background: #ef4444; color: white; }
        .refresh-info {
            text-align: right;
            font-size: 0.75rem;
            color: #64748b;
            margin-bottom: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
            <a href="__VPS_MONITOR_URL__" style="color:#64748b;text-decoration:none;font-size:0.9rem;">&larr; Back to VPS Monitor</a>
            <h1 style="margin:0;">
                <span class="status-dot" id="statusDot"></span>
                Internet Monitor
            </h1>
            <button id="speedTestBtn" onclick="triggerSpeedTest()" style="background:#3b82f6;color:white;border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:0.9rem;">Run Speed Test</button>
        </div>

        <div class="refresh-info">Auto-refreshes every 30 seconds | Last update: <span id="lastUpdate">-</span></div>

        <div class="time-selector">
            <button class="time-btn" data-hours="1">1h</button>
            <button class="time-btn" data-hours="6">6h</button>
            <button class="time-btn active" data-hours="24">24h</button>
            <button class="time-btn" data-hours="168">7d</button>
            <button class="time-btn" data-hours="720">30d</button>
            <button class="time-btn" data-hours="4380">6mo</button>
            <button class="time-btn" data-hours="8760">12mo</button>
        </div>

        <div class="grid">
            <div class="card">
                <h2>Status</h2>
                <div class="stat-value" id="statusText">-</div>
                <div class="stat-sub" id="lastHeartbeat">Last heartbeat: -</div>
            </div>
            <div class="card">
                <h2>Avg Speed</h2>
                <div class="stat-value"><span id="avgSpeed">-</span><span class="stat-unit">Mbps</span></div>
                <div class="stat-sub" id="speedRange">-</div>
            </div>
            <div class="card">
                <h2>Outages</h2>
                <div class="stat-value" id="outageCount">-</div>
                <div class="stat-sub" id="totalDowntime">Total downtime: -</div>
            </div>
            <div class="card">
                <h2>High Latency Events</h2>
                <div class="stat-value" id="latencyCount">-</div>
                <div class="stat-sub">Above threshold</div>
            </div>
        </div>

        <div class="chart-container">
            <h2>Download Speed Over Time</h2>
            <div class="chart-wrapper">
                <canvas id="speedChart"></canvas>
            </div>
        </div>

        <div class="grid">
            <div class="chart-container" style="margin-bottom: 0;">
                <h2>Upload Speed Over Time</h2>
                <div class="chart-wrapper">
                    <canvas id="uploadChart"></canvas>
                </div>
            </div>
            <div class="chart-container" style="margin-bottom: 0;">
                <h2>Latency Events</h2>
                <div class="chart-wrapper">
                    <canvas id="latencyChart"></canvas>
                </div>
            </div>
        </div>

        <div class="chart-container">
            <h2>Recent Events</h2>
            <div class="events-list" id="eventsList">
                <div class="no-data">No events yet</div>
            </div>
        </div>

        <div class="chart-container">
            <h2>Incidents</h2>
            <div id="incidentsList" style="max-height:400px;overflow-y:auto;">
                <div class="no-data">No incidents yet</div>
            </div>
        </div>
    </div>

    <script>
        let speedChart, uploadChart, latencyChart;
        let selectedHours = 24;
        const basePath = window.location.pathname.replace(/\/$/, '') || '';

        const chartOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    type: 'time',
                    time: { displayFormats: { hour: 'MMM d, HH:mm' } },
                    grid: { color: '#334155' },
                    ticks: { color: '#64748b' }
                },
                y: {
                    grid: { color: '#334155' },
                    ticks: { color: '#64748b' }
                }
            }
        };

        function initCharts() {
            const speedCtx = document.getElementById('speedChart').getContext('2d');
            speedChart = new Chart(speedCtx, {
                type: 'line',
                data: { datasets: [{ label: 'Download', borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', fill: true, tension: 0.3, data: [] }] },
                options: { ...chartOptions, scales: { ...chartOptions.scales, y: { ...chartOptions.scales.y, title: { display: true, text: 'Mbps', color: '#64748b' } } } }
            });

            const uploadCtx = document.getElementById('uploadChart').getContext('2d');
            uploadChart = new Chart(uploadCtx, {
                type: 'line',
                data: { datasets: [{ label: 'Upload', borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)', fill: true, tension: 0.3, data: [] }] },
                options: { ...chartOptions, scales: { ...chartOptions.scales, y: { ...chartOptions.scales.y, title: { display: true, text: 'Mbps', color: '#64748b' } } } }
            });

            const latencyCtx = document.getElementById('latencyChart').getContext('2d');
            latencyChart = new Chart(latencyCtx, {
                type: 'scatter',
                data: { datasets: [{ label: 'Latency', borderColor: '#f59e0b', backgroundColor: '#f59e0b', data: [] }] },
                options: { ...chartOptions, scales: { ...chartOptions.scales, y: { ...chartOptions.scales.y, title: { display: true, text: 'ms', color: '#64748b' } } } }
            });
        }

        async function fetchData() {
            try {
                const [summaryRes, historyRes] = await Promise.all([
                    fetch(`${basePath}/api/summary?hours=${selectedHours}`),
                    fetch(`${basePath}/api/history?hours=${selectedHours}`)
                ]);

                const summary = await summaryRes.json();
                const history = await historyRes.json();

                updateStatus(summary);
                updateCharts(history.events);
                updateEventsList(history.events);
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
            } catch (err) {
                console.error('Failed to fetch data:', err);
            }
        }

        function updateStatus(summary) {
            const isOnline = summary.status.is_online;
            document.getElementById('statusDot').className = 'status-dot ' + (isOnline ? 'online' : 'offline');
            document.getElementById('statusText').textContent = isOnline ? 'Online' : 'Offline';

            if (summary.status.last_heartbeat_age_seconds) {
                const age = Math.round(summary.status.last_heartbeat_age_seconds);
                document.getElementById('lastHeartbeat').textContent = `Last heartbeat: ${age}s ago`;
            }

            const stats = summary.speed_stats;
            document.getElementById('avgSpeed').textContent = stats.average ?? '-';
            document.getElementById('speedRange').textContent = stats.min && stats.max ?
                `Min: ${stats.min} / Max: ${stats.max} Mbps (${stats.test_count} tests)` : `${stats.test_count} tests`;

            document.getElementById('outageCount').textContent = summary.outage_stats.count;
            const downtime = summary.outage_stats.total_downtime_seconds;
            if (downtime > 3600) {
                document.getElementById('totalDowntime').textContent = `Total downtime: ${(downtime/3600).toFixed(1)}h`;
            } else if (downtime > 60) {
                document.getElementById('totalDowntime').textContent = `Total downtime: ${(downtime/60).toFixed(1)}m`;
            } else {
                document.getElementById('totalDowntime').textContent = `Total downtime: ${downtime}s`;
            }

            document.getElementById('latencyCount').textContent = summary.latency_events;
        }

        function updateCharts(events) {
            const speedData = [], uploadData = [], latencyData = [];

            events.forEach(e => {
                const x = new Date(e.timestamp * 1000);
                if (e.event_type === 'speed_test') {
                    if (e.data.speed_mbps) speedData.push({ x, y: e.data.speed_mbps });
                    if (e.data.upload_mbps) uploadData.push({ x, y: e.data.upload_mbps });
                } else if ((e.event_type === 'high_latency' || e.event_type === 'latency') && e.data.ping_ms) {
                    latencyData.push({ x, y: e.data.ping_ms });
                }
            });

            speedChart.data.datasets[0].data = speedData.reverse();
            speedChart.update();

            uploadChart.data.datasets[0].data = uploadData.reverse();
            uploadChart.update();

            latencyChart.data.datasets[0].data = latencyData.reverse();
            latencyChart.update();
        }

        function updateEventsList(events) {
            const list = document.getElementById('eventsList');
            if (!events.length) {
                list.innerHTML = '<div class="no-data">No events in selected period</div>';
                return;
            }

            list.innerHTML = events.slice(0, 50).map(e => {
                const time = new Date(e.timestamp * 1000).toLocaleString();
                let detail = '';
                if (e.event_type === 'speed_test') {
                    const testType = e.data.test_type || (e.data.ookla_speed_mbps ? 'ookla' : 'vps'); const trigger = e.data.trigger || '?'; detail = `${e.data.speed_mbps?.toFixed(1) ?? '-'} Mbps`;
                    if (e.data.upload_mbps) detail += ` ↑${e.data.upload_mbps.toFixed(1)}`;
                    detail += ` <span style="opacity:0.6;font-size:0.75em">[${testType}/${trigger}]</span>`;
                } else if (e.event_type === 'high_latency' || e.event_type === 'latency') {
                    detail = `${e.data.ping_ms?.toFixed(0) ?? '-'} ms`;
                } else if (e.event_type === 'restored' || e.event_type === 'outage_end') {
                    const dur = e.data.duration_seconds;
                    detail = dur > 60 ? `${(dur/60).toFixed(1)} min` : `${dur?.toFixed(0) ?? '-'}s`;
                } else if (e.event_type === 'down' || e.event_type === 'outage_start') {
                    detail = e.data.reason || 'Connection lost';
                }
                return `<div class="event-item">
                    <div><span class="event-type ${e.event_type}">${e.event_type.replace('_', ' ')}</span> ${detail}</div>
                    <div class="event-time">${time}</div>
                </div>`;
            }).join('');
        }

        document.querySelectorAll('.time-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                selectedHours = parseInt(btn.dataset.hours);
                fetchData();
                fetchIncidents();
            });
        });

        
        async function triggerSpeedTest() {
            const btn = document.getElementById('speedTestBtn');
            const origText = btn.textContent;
            btn.disabled = true;
            btn.textContent = 'Running...';
            btn.style.background = '#64748b';
            try {
                const resp = await fetch(basePath + '/trigger-speedtest?type=ookla', {method: 'POST'});
                const data = await resp.json();
                if (data.error) {
                    alert('Speed test failed: ' + data.error);
                } else {
                    const speed = data.download_mbps || data.speed_mbps || '--';
                    alert('Speed test complete! Download: ' + speed + ' Mbps');
                    fetchData();
                }
            } catch (e) {
                alert('Error: ' + e.message);
            }
            btn.disabled = false;
            btn.textContent = origText;
            btn.style.background = '#3b82f6';
        }

        async function fetchIncidents() {
            try {
                const res = await fetch(`${basePath}/api/incidents?hours=${selectedHours}`);
                const data = await res.json();
                const container = document.getElementById('incidentsList');
                if (!data.incidents || !data.incidents.length) {
                    container.innerHTML = '<div class="no-data">No incidents in selected period</div>';
                    return;
                }
                let html = '<table class="incidents-table"><thead><tr><th>Time</th><th>Type</th><th>Cause</th><th>Details</th><th>Retest</th><th>Resolved</th></tr></thead><tbody>';
                data.incidents.forEach(inc => {
                    const time = new Date(inc.timestamp * 1000).toLocaleString();
                    let typeBadge, cause, details, retest, resolved;

                    // Determine cause display
                    if (inc.cause === 'power_cut') {
                        cause = '<span class="badge" style="background:#a855f7">power cut</span>';
                    } else if (inc.cause === 'isp_issue') {
                        cause = '<span class="badge" style="background:#3b82f6">ISP issue</span>';
                    } else if (inc.type === 'slow_speed') {
                        cause = '<span style="color:#64748b">—</span>';
                    } else {
                        cause = '<span style="color:#64748b">unknown</span>';
                    }

                    if (inc.merged) {
                        typeBadge = '<span class="badge outage">outage</span>';
                        details = inc.summary;
                        retest = '<span style="color:#64748b">' + inc.sub_count + ' events</span>';
                        resolved = inc.resolved_at ? new Date(inc.resolved_at * 1000).toLocaleString() : '<span style="color:#ef4444">ongoing</span>';
                    } else if (inc.type === 'outage') {
                        typeBadge = '<span class="badge outage">outage</span>';
                        const dur = inc.duration_seconds;
                        if (dur > 3600) details = (dur/3600).toFixed(1) + 'h down';
                        else if (dur > 60) details = (dur/60).toFixed(1) + 'm down';
                        else details = (dur || 0).toFixed(0) + 's down';
                        retest = '<span style="color:#64748b">N/A</span>';
                        resolved = inc.resolved_at ? new Date(inc.resolved_at * 1000).toLocaleString() : '<span style="color:#ef4444">ongoing</span>';
                    } else {
                        typeBadge = '<span class="badge slow">slow speed</span>';
                        details = (inc.speed_mbps || 0).toFixed(1) + ' Mbps';
                        if (inc.trigger) details += ' <span style="opacity:0.6;font-size:0.8em">[' + inc.trigger + ']</span>';
                        if (inc.retest) {
                            const cls = inc.retest.passed ? 'pass' : 'fail';
                            const label = inc.retest.passed ? 'PASS' : 'FAIL';
                            retest = '<span class="badge ' + cls + '">' + label + '</span> ' + (inc.retest.speed_mbps || 0).toFixed(1) + ' Mbps';
                            resolved = inc.retest.passed ? new Date(inc.retest.timestamp * 1000).toLocaleString() : '<span style="color:#ef4444">not resolved</span>';
                        } else {
                            retest = '<span style="color:#64748b">no retest</span>';
                            resolved = inc.resolved_at ? new Date(inc.resolved_at * 1000).toLocaleString() : '<span style="color:#64748b">unknown</span>';
                        }
                    }
                    html += '<tr><td>' + time + '</td><td>' + typeBadge + '</td><td>' + cause + '</td><td>' + details + '</td><td>' + retest + '</td><td>' + resolved + '</td></tr>';
                });
                html += '</tbody></table>';
                container.innerHTML = html;
            } catch (err) {
                console.error('Failed to fetch incidents:', err);
            }
        }

        initCharts();
        fetchData();
        fetchIncidents();
        setInterval(() => { fetchData(); fetchIncidents(); }, 30000);
    </script>
</body>
</html>
'''


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
    speedtest_file: str = './speedtest/10MB.bin'
    database_file: str = './monitor.db'
    # Grace period after startup before sending DOWN notifications
    startup_grace_seconds: int = 120
    vps_monitor_url: str = ''

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
            'DATABASE_FILE': ('database_file', str),
            'VPS_MONITOR_URL': ('vps_monitor_url', str),
        }

        for env_var, (attr, converter) in env_mappings.items():
            value = os.environ.get(env_var)
            if value:
                try:
                    setattr(config, attr, converter(value))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid value for {env_var}: {e}")

        return config


class EventStore:
    """SQLite-based event storage for dashboard history."""

    def __init__(self, config: Config):
        self.db_path = Path(config.database_file)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection (thread-safe)."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize database tables."""
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    datetime TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    source TEXT DEFAULT 'vps'
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)')
            conn.commit()

    def add_event(self, event_type: str, data: dict, source: str = 'vps'):
        """Add an event to the store."""
        timestamp = data.get('timestamp', time.time())
        datetime_str = data.get('datetime_str', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        with self._get_connection() as conn:
            conn.execute(
                'INSERT INTO events (timestamp, datetime, event_type, data, source) VALUES (?, ?, ?, ?, ?)',
                (timestamp, datetime_str, event_type, json.dumps(data), source)
            )
            conn.commit()

    def get_events(self, event_type: str = None, hours: int = 24, limit: int = 1000) -> list:
        """Get events from the store."""
        cutoff = time.time() - (hours * 3600)
        with self._get_connection() as conn:
            if event_type:
                cursor = conn.execute(
                    'SELECT * FROM events WHERE event_type = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?',
                    (event_type, cutoff, limit)
                )
            else:
                cursor = conn.execute(
                    'SELECT * FROM events WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?',
                    (cutoff, limit)
                )
            rows = cursor.fetchall()
            return [
                {
                    'id': row['id'],
                    'timestamp': row['timestamp'],
                    'datetime': row['datetime'],
                    'event_type': row['event_type'],
                    'data': json.loads(row['data']),
                    'source': row['source']
                }
                for row in rows
            ]

    def get_uptime_periods(self, hours: int = 24) -> list:
        """Get uptime/downtime periods for timeline visualization."""
        events = self.get_events(hours=hours, limit=5000)
        # Filter to status change events
        status_events = [e for e in events if e['event_type'] in ('down', 'restored', 'heartbeat_first')]
        status_events.sort(key=lambda x: x['timestamp'])
        return status_events

    def get_speed_tests(self, hours: int = 168) -> list:  # 7 days default
        """Get speed test results."""
        return self.get_events(event_type='speed_test', hours=hours)

    def get_latency_events(self, hours: int = 24) -> list:
        """Get high latency events."""
        return self.get_events(event_type='high_latency', hours=hours)

    def get_outages(self, hours: int = 168) -> list:  # 7 days default
        """Get outage events."""
        events = self.get_events(hours=hours)
        return [e for e in events if e['event_type'] in ('outage_start', 'outage_end', 'down', 'restored')]

    def cleanup_old_events(self, days: int = 30):
        """Delete events older than specified days."""
        cutoff = time.time() - (days * 86400)
        with self._get_connection() as conn:
            conn.execute('DELETE FROM events WHERE timestamp < ?', (cutoff,))
            conn.commit()


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
        # Track boot_id to detect NAS reboots (power cuts)
        self.last_boot_id: Optional[str] = None
        self.boot_id_before_outage: Optional[str] = None

    def record_heartbeat(self, data: dict):
        """Record a received heartbeat."""
        with self._lock:
            now = time.time()
            was_offline = not self.is_online
            new_boot_id = data.get('boot_id', '')
            new_uptime = data.get('uptime_seconds', 0)

            self.last_heartbeat_time = now
            self.last_heartbeat_data = data
            self.is_online = True

            if was_offline and self.outage_start_time:
                # Calculate outage duration
                duration = now - self.outage_start_time
                self.outage_start_time = None

                # Detect if NAS rebooted during outage
                nas_rebooted = False
                if self.boot_id_before_outage and new_boot_id:
                    nas_rebooted = (new_boot_id != self.boot_id_before_outage)

                self.last_boot_id = new_boot_id
                self.boot_id_before_outage = None

                return {
                    'event': 'restored',
                    'duration_seconds': duration,
                    'nas_rebooted': nas_rebooted,
                    'boot_id': new_boot_id,
                    'uptime_seconds': new_uptime,
                }

            # Update boot_id tracking
            self.last_boot_id = new_boot_id
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
                    self.boot_id_before_outage = self.last_boot_id
                    return {'event': 'down', 'reason': 'no_heartbeat_received'}
                return None

            # Check if heartbeat is overdue
            time_since_heartbeat = now - self.last_heartbeat_time
            if time_since_heartbeat > self.config.heartbeat_timeout_seconds:
                if self.is_online:
                    self.is_online = False
                    self.outage_start_time = self.last_heartbeat_time
                    self.boot_id_before_outage = self.last_boot_id
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
        self.event_store = EventStore(config)

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
                    nas_rebooted = result.get('nas_rebooted', False)
                    cause = 'power_cut' if nas_rebooted else 'isp_issue'
                    logger.info(f"Home network RESTORED after {duration:.1f}s (cause: {cause})")
                    self.notifier.notify_restored(duration)
                    self.outage_logger.log_outage({
                        'type': 'restored',
                        'duration_seconds': duration,
                        'nas_rebooted': nas_rebooted,
                    })
                    self.event_store.add_event('restored', {
                        'timestamp': time.time(),
                        'datetime_str': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'duration_seconds': duration,
                        'nas_rebooted': nas_rebooted,
                        'boot_id': result.get('boot_id', ''),
                        'uptime_seconds': result.get('uptime_seconds', 0),
                    }, source='vps')
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
                self.event_store.add_event('outage_report', data, source='nas')

                return jsonify({'status': 'ok'})

            except Exception as e:
                logger.error(f"Error processing outage report: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

        @self.app.route('/api/events', methods=['POST'])
        def receive_events():
            """Receive events from NAS (speed tests, latency, etc)."""
            try:
                data = request.get_json() or {}
                event_type = data.get('event_type', 'unknown')
                event_data = data.get('data', {})
                event_data['received_at'] = time.time()

                self.event_store.add_event(event_type, event_data, source='nas')
                logger.debug(f"Event received from NAS: {event_type}")

                return jsonify({'status': 'ok'})
            except Exception as e:
                logger.error(f"Error receiving event: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

        @self.app.route('/api/history', methods=['GET'])
        def get_history():
            """Get event history for dashboard."""
            hours = request.args.get('hours', 168, type=int)  # 7 days default
            event_type = request.args.get('type', None)

            if event_type:
                events = self.event_store.get_events(event_type=event_type, hours=hours)
            else:
                events = self.event_store.get_events(hours=hours)

            return jsonify({
                'events': events,
                'count': len(events),
                'hours': hours
            })

        @self.app.route('/api/summary', methods=['GET'])
        def get_summary():
            """Get summary data for dashboard."""
            hours = request.args.get('hours', 24, type=int)

            speed_tests = self.event_store.get_speed_tests(hours=hours)
            outages = self.event_store.get_outages(hours=hours)
            latency_events = self.event_store.get_latency_events(hours=hours)
            status = self.tracker.get_status()

            # Calculate stats
            speeds = [e['data'].get('speed_mbps') for e in speed_tests if e['data'].get('speed_mbps')]
            avg_speed = sum(speeds) / len(speeds) if speeds else None
            min_speed = min(speeds) if speeds else None
            max_speed = max(speeds) if speeds else None

            outage_count = len([e for e in outages if e['event_type'] in ('outage_start', 'down')])
            total_downtime = sum(
                e['data'].get('duration_seconds', 0)
                for e in outages
                if e['event_type'] in ('outage_end', 'restored', 'outage_report')
            )

            return jsonify({
                'status': status,
                'speed_stats': {
                    'average': round(avg_speed, 1) if avg_speed else None,
                    'min': round(min_speed, 1) if min_speed else None,
                    'max': round(max_speed, 1) if max_speed else None,
                    'test_count': len(speed_tests)
                },
                'outage_stats': {
                    'count': outage_count,
                    'total_downtime_seconds': total_downtime
                },
                'latency_events': len(latency_events),
                'hours': hours
            })

        @self.app.route('/api/incidents', methods=['GET'])
        def get_incidents():
            """Get incident list (outages + slow speed tests) for dashboard."""
            hours = request.args.get('hours', 24, type=int)
            events = self.event_store.get_events(hours=hours)

            raw_incidents = []

            # Collect outage incidents (pair down + restored)
            down_events = [e for e in events if e['event_type'] in ('down', 'outage_start')]
            restored_events = [e for e in events if e['event_type'] in ('restored', 'outage_end')]
            restored_events.sort(key=lambda x: x['timestamp'])

            for de in down_events:
                restored = None
                for re in restored_events:
                    if re['timestamp'] > de['timestamp']:
                        restored = re
                        break
                duration = restored['data'].get('duration_seconds', 0) if restored else None
                resolved_at = restored['timestamp'] if restored else None
                nas_rebooted = restored['data'].get('nas_rebooted') if restored else None
                # Determine cause: power_cut if NAS rebooted, isp_issue if not, unknown if no data
                if nas_rebooted is True:
                    cause = 'power_cut'
                elif nas_rebooted is False:
                    cause = 'isp_issue'
                else:
                    cause = 'unknown'
                raw_incidents.append({
                    'type': 'outage',
                    'timestamp': de['timestamp'],
                    'datetime': de['datetime'],
                    'duration_seconds': duration,
                    'reason': de['data'].get('reason', 'Connection lost'),
                    'resolved_at': resolved_at,
                    'cause': cause,
                    'nas_rebooted': nas_rebooted,
                })

            # Collect slow speed test incidents (speed_mbps < 50)
            speed_tests = [e for e in events if e['event_type'] == 'speed_test']
            speed_tests.sort(key=lambda x: x['timestamp'])

            for i, st in enumerate(speed_tests):
                speed = st['data'].get('speed_mbps')
                if speed is None or speed >= 50:
                    continue
                trigger = st['data'].get('trigger', '')
                if trigger == 'slow_speed_retest':
                    continue

                incident = {
                    'type': 'slow_speed',
                    'timestamp': st['timestamp'],
                    'datetime': st['datetime'],
                    'speed_mbps': speed,
                    'trigger': trigger,
                    'retest': None,
                }

                for j in range(i + 1, len(speed_tests)):
                    candidate = speed_tests[j]
                    dt = candidate['timestamp'] - st['timestamp']
                    if dt > 900:
                        break
                    if candidate['data'].get('trigger') == 'slow_speed_retest':
                        retest_speed = candidate['data'].get('speed_mbps', 0)
                        incident['retest'] = {
                            'speed_mbps': retest_speed,
                            'passed': retest_speed >= 50,
                            'timestamp': candidate['timestamp'],
                        }
                        break

                raw_incidents.append(incident)

            # Sort chronologically for grouping
            raw_incidents.sort(key=lambda x: x['timestamp'])

            # Group incidents within 30 min of each other into single events
            MERGE_GAP = 1800  # 30 minutes
            groups = []
            for inc in raw_incidents:
                if groups:
                    last = groups[-1]
                    last_end = last['resolved_at'] or last['sub_incidents'][-1]['timestamp']
                    if inc['timestamp'] - last_end <= MERGE_GAP:
                        last['sub_incidents'].append(inc)
                        # Update resolved_at to latest
                        inc_end = inc.get('resolved_at') or (inc.get('retest', {}) or {}).get('timestamp')
                        if inc_end and (last['resolved_at'] is None or inc_end > last['resolved_at']):
                            last['resolved_at'] = inc_end
                        continue
                # Start new group
                resolved = inc.get('resolved_at') or (inc.get('retest', {}) or {}).get('timestamp')
                groups.append({
                    'resolved_at': resolved,
                    'sub_incidents': [inc],
                })

            # Build final incident list from groups
            incidents = []
            for g in groups:
                subs = g['sub_incidents']
                outages = [s for s in subs if s['type'] == 'outage']
                slows = [s for s in subs if s['type'] == 'slow_speed']
                total_downtime = sum(s.get('duration_seconds') or 0 for s in outages)

                if len(subs) == 1:
                    # Single incident, pass through as-is with resolved_at
                    entry = dict(subs[0])
                    if entry['type'] == 'slow_speed' and entry.get('retest') and entry['retest'].get('passed'):
                        entry['resolved_at'] = entry['retest']['timestamp']
                    elif entry['type'] == 'slow_speed':
                        entry['resolved_at'] = None
                    incidents.append(entry)
                else:
                    # Merged group
                    parts = []
                    if outages:
                        parts.append(f"{len(outages)} outage{'s' if len(outages) != 1 else ''}")
                    if slows:
                        parts.append(f"{len(slows)} slow test{'s' if len(slows) != 1 else ''}")
                    summary = ', '.join(parts)

                    if total_downtime > 3600:
                        summary += f" — {total_downtime/3600:.1f}h total downtime"
                    elif total_downtime > 60:
                        summary += f" — {total_downtime/60:.1f}m total downtime"
                    elif total_downtime > 0:
                        summary += f" — {total_downtime:.0f}s total downtime"

                    # Determine cause for merged group (use last outage's cause)
                    group_cause = 'unknown'
                    for s in reversed(outages):
                        if s.get('cause') and s['cause'] != 'unknown':
                            group_cause = s['cause']
                            break

                    incidents.append({
                        'type': 'outage' if outages else 'slow_speed',
                        'timestamp': subs[0]['timestamp'],
                        'datetime': subs[0]['datetime'],
                        'merged': True,
                        'summary': summary,
                        'resolved_at': g['resolved_at'],
                        'sub_count': len(subs),
                        'total_downtime_seconds': total_downtime,
                        'cause': group_cause,
                    })

            incidents.sort(key=lambda x: x['timestamp'], reverse=True)

            return jsonify({
                'incidents': incidents,
                'count': len(incidents),
                'hours': hours,
            })

        @self.app.route('/status', methods=['GET'])
        def status():
            """Get current status."""
            return jsonify(self.tracker.get_status())

        @self.app.route('/health', methods=['GET'])
        def health():
            """Health check endpoint."""
            return jsonify({'status': 'healthy', 'timestamp': time.time()})

        @self.app.route('/', methods=['GET'])
        @self.app.route('/dashboard', methods=['GET'])
        def dashboard():
            """Serve the dashboard HTML."""
            html = DASHBOARD_HTML.replace('__VPS_MONITOR_URL__', self.config.vps_monitor_url)
            return Response(html, mimetype='text/html')

        @self.app.route('/speedtest', methods=['GET'])
        def speedtest():
            """Serve speedtest file for download speed measurement."""
            speedtest_path = Path(self.config.speedtest_file)
            if not speedtest_path.exists():
                return jsonify({'error': 'Speedtest file not found'}), 404
            return send_file(
                speedtest_path,
                mimetype='application/octet-stream',
                as_attachment=True,
                download_name='speedtest.bin'
            )
        @self.app.route('/trigger-speedtest', methods=['POST'])
        def trigger_speedtest():
            """Trigger a speed test on the NAS."""
            import urllib.request
            import urllib.error
            nas_url = "http://100.66.41.139:8090"
            test_type = request.args.get('type', 'ookla')  # ookla, vps, or full
            
            try:
                if test_type == 'full':
                    endpoint = f"{nas_url}/speedtest/full"
                elif test_type == 'ookla':
                    endpoint = f"{nas_url}/speedtest/ookla"
                else:
                    endpoint = f"{nas_url}/speedtest"
                
                req = urllib.request.Request(endpoint, method='GET')
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    return Response(data, mimetype='application/json')
            except urllib.error.URLError as e:
                return jsonify({'error': f'Failed to reach NAS: {str(e)}'}), 502
            except Exception as e:
                return jsonify({'error': str(e)}), 500


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
                        self.event_store.add_event('down', {
                            'timestamp': time.time(),
                            'datetime_str': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'reason': reason
                        }, source='vps')
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
