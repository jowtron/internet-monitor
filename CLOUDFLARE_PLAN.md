# Plan: move vps-monitor to Cloudflare Workers

Status: **planned, not built** (noted 2026-10-19). The PBX VPS (`100.89.202.1`) that runs `vps-heartbeat` is due to be rebuilt or deleted, so the VPS half of this project needs a new home. This is the plan for that home. Nothing here has been built or deployed yet.

## Goals

- Replace `vps-monitor` (Flask + SQLite on the PBX VPS) with a Cloudflare Worker, so the outside-the-house watcher no longer depends on any box Joseph runs.
- Switch notifications from ntfy to **Pushover**, for both halves (VPS alerts and the NAS's speed-test alerts).
- Keep the dashboard, history and incident view, and carry the existing history across.
- Keep the NAS side (`nas-monitor`) as close to unchanged as possible.

## What the VPS does today (what has to be replaced)

| Today (`vps-monitor/monitor.py`) | On Cloudflare |
|---|---|
| `POST /heartbeat` every 60s from the NAS; a 30s check loop sends DOWN after 180s of silence and RESTORED when heartbeats resume (with NAS reboot detection from `boot_id` / `uptime_seconds`) | Durable Object alarm, re-armed on every heartbeat (see below) |
| `POST /outage`: the NAS reports its own ping-based outages after it recovers | Same endpoint, stored as events |
| `POST /api/events`: speed tests, latency, high latency | Same endpoint, stored as events |
| SQLite `events` table (no retention cleanup is ever called, so it keeps everything) | SQLite inside the Durable Object, same schema |
| `GET /`, `/dashboard`, `/api/history`, `/api/summary`, `/api/incidents`, `/status`, `/health` | Same routes, ported to TypeScript; dashboard HTML moved over almost verbatim |
| `GET /speedtest` serves `speedtest/10MB.bin`; this is the NAS's **hourly scheduled speed test** and the trigger for slow-speed mode | 10MB object in R2, served by the Worker |
| `POST /trigger-speedtest` calls the NAS at `100.66.41.139:8090` over Tailscale and waits for the result | Queued command, picked up by the NAS on its next heartbeat |
| ntfy (`jowtron-home-network`) for DOWN / RESTORED | Pushover |
| Reached over Tailscale; dashboard via `tailscale serve` at `incrediblepbx.merino-komodo.ts.net/monitor/internet` | Public hostname behind Cloudflare Access |

## Architecture

```
NAS (nas-monitor) ──HTTPS + Access service token──> Worker ──> Durable Object "home"
                                                      │            ├─ SQLite: events, state
Joseph's browser ──Cloudflare Access──> dashboard ────┘            ├─ alarm: heartbeat timeout
                                                                   └─> Pushover
                                                     R2: speedtest/10MB.bin
```

### One Worker, one Durable Object

- A single Durable Object instance (`idFromName("home")`) owns all state. It is the only thing that needs to be consistent, and a DO gives both SQLite storage and alarms in one place, so no D1 database or cron trigger is needed.
- **Down detection:** on every heartbeat the DO stores `last_heartbeat` and calls `setAlarm(now + 180s)`. If the alarm fires, no heartbeat arrived in time: mark offline, record a `down` event, push DOWN. The next heartbeat records `restored`, works out the duration and pushes RESTORED (with "NAS rebooted" when `boot_id` changed, as today). This replaces the polling loop and the startup grace period; there is no process restart for the grace period to cover.
- **Storage:** port the `events` table as is (`id, timestamp, event_type, source, data JSON`) plus a small `state` table (`is_online`, `last_heartbeat`, `outage_start`, `last_boot_id`, `pending_commands`). 9 months of history is 3.6MB, so keep everything, as today.
- **Dashboard and APIs:** the Worker routes read requests to the DO, which runs the same queries the Flask app runs. The incident grouping in `/api/incidents` (pair down/restored, slow-speed tests with their retest, group within 30 minutes) ports line for line.

### Notifications: Pushover

- A Pushover **application** for this project (e.g. "Home Internet"), with its token and Joseph's user key stored as Worker secrets `PUSHOVER_TOKEN` and `PUSHOVER_USER`. Free up to 10,000 messages a month per app.
- The Worker sends **every** push, including the NAS's speed-test alerts, so the Pushover token lives in one place and the NAS holds none. The NAS replaces its `NtfyNotifier` with a small client that POSTs `{title, message, priority}` to `POST /api/notify`; the decision logic for *when* to notify stays on the NAS where it already is.
- Priority mapping from today's ntfy levels:

| Message | ntfy today | Pushover |
|---|---|---|
| Home Network DOWN | high | `1` (high; bypasses quiet hours) |
| Home Network RESTORED | default | `0` |
| Slow speed confirmed / slow retest | high | `1` |
| Manual speed test result, speed recovered | default | `0` |

- The DOWN wording should say "home internet or NAS down": a missing heartbeat can't tell a dead connection from a powered-off NAS, and the reboot detection on RESTORED is what separates them afterwards.

### Speed test file

- Upload `10MB.bin` to an R2 bucket and stream it from the Worker at `GET /speedtest` with `Cache-Control: no-store` and no compression, so it measures the line rather than a cache or gzip. R2 has no egress fees, and the hourly test plus 5-minute slow-mode tests are far inside the free operations tier.
- This measures home-to-Cloudflare-Sydney rather than home-to-VPS. That's a slightly different number but just as good for spotting a slow line, and the Ookla test that confirms slow results is unchanged.
- Generating random bytes in the Worker instead of R2 was ruled out: 10MB of random data costs more CPU than the free plan allows per request.

### "Run Speed Test" button

- A Worker can't reach the NAS on the tailnet. Rather than adding a cloudflared tunnel and a VPC service on the NAS, the button queues a command (`POST /trigger-speedtest?type=ookla` stores `pending_commands` in the DO), and the **heartbeat response** carries it: `{"ok": true, "commands": ["speedtest:ookla"]}`. The NAS runs it and posts the result through `/api/events` as it already does.
- Latency: up to one heartbeat interval (60s) plus the test itself. The dashboard changes from a blocking `fetch` followed by `alert()` to "Queued, the result will appear here in a minute or two", polling `/api/history` until a `manual` speed test newer than the click shows up.
- Nothing has to reach into the home network, so the NAS's HTTP server on `:8090` becomes optional. Keep it for triggering tests from the LAN.

### Auth and hostname

- Custom domain on a zone Joseph already has, e.g. `netmon.jderrick.app` (name to be decided). Set `workers_dev = false` so the `*.workers.dev` URL can't bypass Access.
- **Cloudflare Access** application over the whole hostname, allowing Joseph's email (free tier). The dashboard and read APIs are then private, as they are behind Tailscale today.
- The NAS authenticates with an **Access service token** (`CF-Access-Client-Id` / `CF-Access-Client-Secret` headers, stored in the NAS's `docker-compose.override.yml`), so there's one auth layer for everything rather than a bypass rule plus a separate shared secret. The Worker additionally checks the `Cf-Access-Jwt-Assertion` header so a misconfigured Access policy fails closed.
- Heartbeats now travel over the public internet instead of Tailscale. That's a feature: today a Tailscale problem looks the same as an internet outage.

### Cost

Within the Workers free plan: about 1,440 heartbeats a day plus events, speed-test downloads and dashboard loads, well under 100k requests a day. SQLite-backed Durable Objects, R2 (10GB storage, free egress), Access (up to 50 users) and Pushover (10k messages a month) are all within free allowances, apart from Pushover's one-off per-platform licence, which Joseph already has.

## Code layout

New directory alongside the existing two, TypeScript:

```
cloudflare-monitor/
  wrangler.jsonc        # DO binding + new_sqlite_classes migration, R2 bucket, custom domain route, workers_dev false
  src/index.ts          # router, Access JWT check, R2 speedtest, forwards to the DO
  src/monitor.ts        # Durable Object: schema, heartbeat/alarm state machine, event queries, incidents
  src/pushover.ts       # Pushover client + priority mapping
  src/dashboard.html    # ported from the inline DASHBOARD_HTML, imported as text
  scripts/import.ts     # one-off history import (below)
  test/                 # vitest-pool-workers: alarm fires → DOWN, heartbeat → RESTORED, reboot detection, incident grouping
```

`vps-monitor/` stays in the repo until the cutover is verified, then gets deleted.

## NAS changes (`nas-monitor/monitor.py`)

1. `VPS_URL` becomes the Worker URL (`https://…`), and every request sends the Access service-token headers (new env vars `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`).
2. Replace `NtfyNotifier` with a notifier that POSTs to `/api/notify`; drop the `NTFY_*` config.
3. Read `commands` from the heartbeat response and run `speedtest:ookla` / `speedtest:full` / `speedtest:quick` through the existing speed-test methods.
4. The quick speed test downloads from `{VPS_URL}/speedtest` already, so it follows the URL change with no code change.
5. Change `restart: unless-stopped` to `restart: always` in `docker-compose.yml`. See the note at the end: the container was stopped on 2026-10-11 and stayed down through a NAS reboot, because `unless-stopped` doesn't restart a container that was explicitly stopped.

## History import

The full VPS history was exported on 2026-10-19 to `data/pbx-vps-export-2026-10-19/` (gitignored, laptop only): `monitor.db` (13,846 events, 2026-01-10 to 2026-10-11, integrity-checked `.backup` copy), plus `outages.log`, `config.yaml` and the `vps-heartbeat.service` unit.

`scripts/import.ts` reads that `monitor.db` locally and POSTs the rows in batches of about 500 to a temporary `POST /admin/import` route, which inserts them with their original timestamps and ids. Remove the route after the import.

The NAS also keeps its own daily CSVs in `/share/Container/nas-monitor/logs/` (latency and speed tests since 2025-12-28), so most of the history exists twice. The VPS export is the only copy of the **outside view**: the `down` / `restored` heartbeat-timeout events, which are the record of the NAS or its power being down.

## Cutover steps

1. Build the Worker, create the R2 bucket and upload a 10MB file of random bytes (`head -c 10485760 /dev/urandom > 10MB.bin`; random so nothing along the way can compress it), set the Pushover secrets, create the Access application and the NAS service token.
2. Deploy, then run the history import and check the dashboard against the export.
3. Update the NAS: new env vars in `docker-compose.override.yml`, then `./update-nas.sh`.
4. Verify end to end:
   - stop the NAS container: a DOWN push arrives within about 3 minutes
   - start it: a RESTORED push arrives with the duration
   - press "Run Speed Test" on the dashboard: the result appears within about 2 minutes
   - the hourly scheduled test shows up in the history
5. Decommission on the VPS (if it still exists): `systemctl disable --now vps-heartbeat`, and remove the `/monitor/internet` `tailscale serve` route. The separate VPS stats dashboard (`/root/vps-monitor`, port 8085), which shows a "Home Internet" summary by proxying to port 5000, goes away with the box. It's a different project and isn't being replaced.
6. Update `README.md`, `CLAUDE.md` and `DEPLOYMENT.md` for the new setup, and delete `vps-monitor/`.

## Open decisions

- Hostname for the Worker.
- Whether to keep the NAS's `:8090` HTTP server once commands come through heartbeats.

## Note: monitoring has been off since 2026-10-11

Found while planning this (2026-10-19): the `nas-monitor` container received a SIGTERM at 2026-10-11 11:27 UTC, exited cleanly (status 0), and was never restarted. The NAS rebooted about 2026-10-13, and `restart: unless-stopped` kept the container down because it had been stopped explicitly. The VPS has reported "down" since then, and no heartbeats or speed tests have been recorded. Restarting the container (and changing its restart policy) is independent of this plan.
