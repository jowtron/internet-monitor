# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **Boot ID tracking** to detect power cuts vs ISP issues
  - NAS heartbeats now include `boot_id` and `uptime_seconds`
  - VPS tracks boot_id changes across outages
  - Incidents show cause: "power cut" (NAS rebooted) or "ISP issue" (NAS stayed up)
- **Incidents section** on dashboard
  - Shows outages and slow speed tests in a unified table
  - Merges related incidents within 30 minutes into single grouped events
  - Displays: Time, Type, Cause, Details, Retest result, Resolved time
- **Extended time ranges**: 6-month and 12-month buttons on time selector
- **Resolved timestamp** for all incidents

### Changed
- Outages now show total downtime when multiple events are grouped
- Slow speed incidents show retest pass/fail status

## [1.0.0] - 2026-01-11

### Added
- Initial release
- NAS monitor with ping-based connectivity detection
- VPS monitor with dashboard and ntfy notifications
- Speed tests (VPS download + Ookla CLI)
- High latency detection with automatic speed test trigger
- Slow speed mode with frequent retests
- Hourly scheduled speed tests
- CSV logging on NAS
- SQLite event storage on VPS
- Docker support for both components
- GitHub Actions for automated Docker builds
