# IIC-FORGE F4 — Exit-gate runbook

> **Historical phase gate.** This runbook documents the F4 exit-gate procedure, now superseded by the Compose-owned runtime. The current production launch/rollback procedure lives in `ops/runbooks/service-platform.md`. Commands below have been updated to the Compose runtime where they remain useful as smoke checks.

> Spec: [docs/superpowers/specs/2026-05-27-iic-forge-07-f4-orchestrator-design.md](../../docs/superpowers/specs/2026-05-27-iic-forge-07-f4-orchestrator-design.md) §9
> Evaluator: [scripts/f4_exit_gate.py](../../scripts/f4_exit_gate.py)

The F4 exit gate has two parts that pass independently:

1. **Synthetic-event smoke** — `pytest tests/smoke/test_f4_exit_gate.py` on the same commit. Must PASS.
2. **Live observation window** — 6–12 h with F3 adapters + F4 promoter+worker running on the dev machine. SLA `p95 ≤ 15 min` (or per the tiered rule when fewer than 3 briefs land).

## Pre-flight checklist

Run sequentially. Any failure → fix before proceeding.

1. **F3 stack healthy.**
   ```bash
   docker compose --profile sources ps
   # Confirm all source adapter services are in state "running".
   docker compose ps triage
   # Confirm triage is in state "running".
   ```

2. **Watchlist non-empty.** The trigger rule requires `ticker ∈ watchlist`.
   ```bash
   docker compose run --rm --entrypoint python triage -m cli.main forge watchlist list
   ```
   If empty: `docker compose run --rm --entrypoint python triage -m cli.main forge watchlist add AAPL` (and the user's other standing tickers).

3. **Tickers reference table seeded.**
   ```bash
   docker compose run --rm --entrypoint python triage -c "import sqlite3; c = sqlite3.connect('/data/iic.db').cursor(); c.execute('SELECT COUNT(*) FROM tickers WHERE active=1'); print(c.fetchone()[0])"
   # Expect ≥ 8000
   ```

4. **All cost guards confirmed OFF.** Gate observes the natural profile.
   ```bash
   docker compose run --rm --entrypoint python triage - <<'EOF'
   from tradingagents.default_config import DEFAULT_CONFIG as C
   for k in ("cost_guard_enabled", "trigger_backpressure_enabled",
             "trigger_daily_rate_enabled", "daily_budget_enabled"):
       assert C[k] is False, f"{k} must be False for the gate"
   print("all guards OFF ✓")
   EOF
   ```

5. **Synthetic smoke passes on the current commit.**
   ```bash
   cd /opt/iic-forge
   docker compose --profile runtime --profile sources --profile dashboard up -d
   python -m pytest tests/smoke/test_f4_exit_gate.py -v
   ```
   Must PASS.

6. **Disable unattended-upgrades for the window.**
   ```bash
   sudo systemctl stop unattended-upgrades.timer
   ```
   Re-enable after the gate completes.

7. **Promoter + worker services started via Compose.**
   ```bash
   # Bring up Redis, then start the full runtime profile (promoter, worker, triage).
   cd /opt/iic-forge
   docker compose up -d redis
   docker compose exec redis redis-cli ping
   docker compose --profile runtime up -d
   # Confirm promoter and worker are running.
   docker compose ps
   ```

## Run procedure

1. **Record `--since` timestamp.**
   ```bash
   export F4_GATE_SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
   echo "$F4_GATE_SINCE" > /tmp/f4_gate_since
   ```

2. **Hold sleep for the window.**
   ```bash
   systemd-inhibit --what=sleep --who="F4 exit gate" \
                   --why="12h orchestrator soak" sleep infinity &
   ```

3. **Walk away.** Recommended window: 12 h.

4. **At window end, run the evaluator.**
   ```bash
   F4_GATE_SINCE=$(cat /tmp/f4_gate_since)
   python scripts/f4_exit_gate.py --since "$F4_GATE_SINCE" --window-hours 12 \
       > docs/superpowers/artifacts/$(date -u +%Y-%m-%d)-f4-exit-gate-report.md
   ```

5. **Review the artifact.** Sign the operator line at the bottom.

6. **Re-enable unattended-upgrades and stop the inhibit:**
   ```bash
   sudo systemctl start unattended-upgrades.timer
   kill %1   # stop systemd-inhibit background job
   ```

## Pass criteria

Cited from spec §9:

- Restart counts for the `promoter`, `worker-action`, and `worker-deep` services must be 0 over the window (`docker compose ps` / `docker inspect --format '{{.RestartCount}}'`). (Restart audit in the artifact.)
- Synthetic-smoke result: PASS (recorded in the artifact alongside the live signal).
- Live SLA (tiered):
  - **≥ 3 briefs** during the window → `p95 latency ≤ 15 min`.
  - **1–2 briefs** → `max latency ≤ 15 min` + operator note confirming the window was "normal".
  - **0 briefs** → not a pass signal; re-run during a more active window.

## Failure modes and recovery

| Symptom | Likely cause | Fix |
|---|---|---|
| Promoter restarts > 0 | unhandled exception in the loop body | `docker compose logs -f promoter` for traceback; the defensive `except Exception` should have swallowed it — file an issue |
| Worker restarts > 0 | OOM during persona fan-out, or an unhandled exception outside `drain_one`'s try/except | check `docker compose logs -f worker` for `Killed (out of memory)`; raise memory limits in `compose.yaml` if needed |
| Latency p95 > 15 min | personas slow, LLM upstream lag, queue backlog | check per-job timing in the artifact; consider falling back to `quick_think_llm` for the synthesis call (open question #2 in the spec) |
| 0 briefs during window | quiet news period or watchlist too small | spec §9 explicitly: re-run during an active window; do not pad with synthetic |
| `error` state jobs | LLM crash, malformed event, timeout | inspect `queue_jobs.error` via `docker compose run --rm --entrypoint python triage -m cli.main forge orchestrator status`; the underlying `runs` rows have artifacts under `data/runs/<run_id>/` |
