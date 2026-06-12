# F3 24h Exit-Gate Runbook

> **Historical phase gate.** This runbook documents the F3 exit-gate procedure, now superseded by the Compose-owned runtime. The current production launch/rollback procedure lives in `ops/runbooks/service-platform.md`. Commands below have been updated to the Compose runtime where they remain useful as smoke checks.

The F3 exit gate is **one contiguous 24-hour run** with **no process restarts
allowed**. A single `systemctl restart` of any sensing or triage unit during
the window invalidates the gate. Plan the start time around your day.

## Pre-flight checklist

Run through this list in order; do not skip. Each item is a `pass/fail` —
fix before continuing.

- [ ] Dev machine on AC power. Battery-only is not acceptable.
- [ ] `systemd-inhibit --what=sleep --who="F3 exit gate" --why="24h sensing soak" sleep infinity &` running.
- [ ] Screen lock disabled for the window (`gsettings set org.gnome.desktop.screensaver lock-enabled false`).
- [ ] Display sleep disabled (`gsettings set org.gnome.desktop.session idle-delay 0`).
- [ ] Unattended-upgrades disabled for the window (`sudo systemctl mask unattended-upgrades.service`). Re-enable after the gate.
- [ ] All six adapters' env vars set: POLYGON_API_KEY, TELEGRAM_API_ID/HASH/SESSION (sensing), X_BEARER_TOKEN (if enabling x), FRED_API_KEY, RSS_FEEDS, GDELT_QUERY.
- [ ] Telegram **sensing** session pre-authenticated: run the telegram adapter once standalone, complete any 2FA prompts, Ctrl+C, then start under Compose.
- [ ] Redis running (`docker compose ps redis`), AOF on (`docker compose exec redis redis-cli CONFIG GET appendonly` → `yes`).
- [ ] `tickers` table seeded: run `docker compose run --rm triage python -m cli.main forge sense reseed-tickers` → at least 8000 rows in `tickers` (or skip the gate and re-seed).
- [ ] Baseline watchlist set: run `docker compose run --rm triage python -m cli.main forge watchlist add` for each of the user's standing tickers.
- [ ] **Pre-soak**: each adapter individually for 1 hour. Each must produce ≥1 event with no restarts:
  ```bash
  docker compose --profile sources up -d
  sleep 3600
  docker compose ps   # confirm all source services still running
  docker compose --profile sources down
  ```
- [ ] No pending reboot: `[ -f /var/run/reboot-required ] && echo NEEDS REBOOT || echo OK`.

## Start

```bash
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "START=$START" | tee /tmp/f3-gate-start.txt

cd /opt/iic-forge

# Bring up Redis first and confirm it is healthy.
docker compose up -d redis
docker compose exec redis redis-cli ping

# Start all sensing adapters and the triage service via Compose profiles.
docker compose --profile sources up -d
docker compose --profile runtime up -d

# Confirm everything is running.
docker compose ps
```

## During the run

- Do not touch any service. Do not run `docker compose restart`.
- Spot-check log volume every few hours: `docker compose logs -f`.
- If a service dies, the gate is invalidated. Note the time; the evaluator
  will flag it. Fix the root cause before re-attempting.

## Stop and evaluate

```bash
START=$(cat /tmp/f3-gate-start.txt | cut -d= -f2)
python scripts/f3_exit_gate.py --since "$START"
# Output: docs/superpowers/artifacts/2026-MM-DD-f3-exit-gate-report.md
```

Review the artifact:
- Auto criteria (events ≥100, no restarts, ≥1 auto-promoted watchlist row) must all be true.
- Spot-check the 30-row dedupe sample. Sign off in the artifact with **YES** or **NO**.

## Tear-down

```bash
docker compose --profile sources --profile runtime down
sudo systemctl unmask unattended-upgrades.service
kill %1  # systemd-inhibit
```
