# IIC-Forge Service Platform Runbook

This is the canonical production launch, cutover, rollback, backup, and focused
soak procedure for the IIC-Forge Compose-based runtime. All commands run from
the repo root (`/opt/iic-forge` on the production host).

---

## Launch

```bash
cd /opt/iic-forge
cp ops/env.iic-forge.example .env
$EDITOR .env
docker compose --profile runtime --profile sources --profile dashboard up -d
python scripts/focused_soak_gate.py --mode preflight --json
```

**Important:** copy `ops/env.iic-forge.example` to `.env` and edit the copy.
Never edit `ops/env.iic-forge.example` in place — it is contract-tested and
committed to the repository.

Check the stack is up:

```bash
docker compose ps
docker compose logs --tail=50 triage promoter
```

---

## Old Service Shutdown

Before the Compose runtime becomes authoritative, disable all legacy per-daemon
host services. The gate checks all 17 units derived from `ops/systemd/*.service`
and `ops/systemd/*.timer` (excluding `iic-forge-compose.service`):

```bash
sudo systemctl disable --now \
  iic-action-handler.service \
  iic-dashboard.service \
  iic-morning.service \
  iic-morning.timer \
  iic-promoter.service \
  iic-sense-gdelt.service \
  iic-sense-macro.service \
  iic-sense-polygon.service \
  iic-sense-rss.service \
  iic-sense-telegram.service \
  iic-sense-x.service \
  iic-telegram-bot.service \
  iic-triage.service \
  iic-watchlist-sweep.service \
  iic-watchlist-sweep.timer \
  iic-worker.service \
  redis-server.service \
  || true
```

Confirm all are inactive:

```bash
systemctl is-active \
  iic-action-handler.service iic-dashboard.service iic-morning.service \
  iic-morning.timer iic-promoter.service iic-sense-gdelt.service \
  iic-sense-macro.service iic-sense-polygon.service iic-sense-rss.service \
  iic-sense-telegram.service iic-sense-x.service iic-telegram-bot.service \
  iic-triage.service iic-watchlist-sweep.service iic-watchlist-sweep.timer \
  iic-worker.service redis-server.service
```

The focused soak gate must report `old_services_stopped: PASS` before
proceeding.

---

## Redis Ownership

Redis is owned by the `redis` service in `compose.yml` backed by the
`iic-forge_iic_redis_data` named volume. Confirm the checked-in config is
loaded and AOF is enabled:

```bash
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli CONFIG GET appendonly
docker compose exec redis redis-cli CONFIG GET maxmemory-policy
```

Expected: `PONG`, `appendonly yes`, and your configured eviction policy.

The focused soak gate check `redis_owned_and_configured` passes only when
`appendonly = yes`.

---

## External Local LLM

The local model server (e.g. llama.cpp server, Ollama, LM Studio) runs
**outside** Compose — it is not a Compose service. On Linux, Docker reaches the
host via `host.docker.internal` resolved to `host-gateway` (already set in
`compose.yml` via `extra_hosts: host.docker.internal:host-gateway`).

Configure only the URL, model name, and provider in `.env`:

```dotenv
LOCAL_LLM_BASE_URL=http://host.docker.internal:8080/v1
IIC_TRIAGE_LLM_PROVIDER=local
IIC_TRIAGE_LLM_MODEL=qwen3.6-27b-instruct-q4_k_m
IIC_ALERT_GATE_LLM_PROVIDER=local
IIC_ALERT_GATE_LLM_MODEL=qwen3.6-27b-instruct-q4_k_m
```

Do not add the local model server as a Compose service. Restart only the
dependent containers after changing local LLM variables:

```bash
docker compose restart triage promoter
```

Verify the gate check `llm_calls_present` is passing after the first triage
cycle. During `--mode preflight` this check is skipped (fresh stack has not
produced evidence yet); it runs in `--mode soak`.

---

## Deferred Salience Retry

Events that fail LLM triage (transport errors, parse errors, endpoint
unavailability) are durably persisted to the `deferred_salience_retry` table
with states `pending`, `running`, `done`, or `dead`. The triage service retries
due rows on each poll cycle without waiting for source republish.

Inspect retry state directly via the volume-mounted SQLite database:

```bash
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('SELECT state, COUNT(*) FROM deferred_salience_retry GROUP BY state')
for row in c.fetchall():
    print(row)
"
```

Healthy launch state: `pending` rows below the gate threshold and no `dead`
rows.

Orphaned events (events in the events table with no salience outcome) are
visible in the dashboard Operations tab and cause the gate check
`deferred_retry_bounded` to fail. The gate detail string includes
`orphaned=<count>` and `oldest_pending_age_seconds=<age>`.

---

## Delivery Fallback

Delivery is ordered: Telegram is attempt rank 1 and email fallback is rank 2.
When a Telegram attempt fails (status `failed`), email is attempted as rank 2
and recorded as a fallback delivery (`is_fallback=1`, `fallback_of=<rank1_id>`).

**Quiet-hours suppression:** when Telegram is skipped with
`skip_reason=quiet_hours` and the brief is not urgent, the delivery policy
returns immediately without attempting email. Skipped-only groups (no `sent`,
no `failed` attempt) are visible in the dashboard Operations tab as
`skipped_only` count but are **not** counted as failures by the gate.

**Urgent flag:** `urgent=True` pierces the quiet-hours short-circuit so a
quiet-hours Telegram skip still falls through to email. The `urgent` parameter
is not yet wired to any producer — all briefs are non-urgent at launch.

Inspect delivery chain history:

```bash
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('''
  SELECT delivery_group_id, attempt_rank, channel, status, fallback_of
  FROM deliveries
  ORDER BY delivery_group_id, attempt_rank
  LIMIT 50
''')
for row in c.fetchall():
    print(row)
"
```

The gate check `delivery_groups_bounded` fails when any delivery group has zero
sent rows and at least one failed attempt.

---

## Worker Lanes

Two worker lanes run as separate Compose services:

- `worker-deep` (`IIC_WORKER_LANE=deep`) — handles full TradingAgents analysis
  jobs. Effective concurrency is 1 per worker (multi-slot loop is future work).
- `worker-action` (`IIC_WORKER_LANE=action`) — handles action-type jobs.
  **Idle by design at launch**: no producer currently routes jobs to
  `lane=action`. The service starts cleanly but processes no work until a
  producer begins routing to the action lane.

Check worker lane status:

```bash
docker compose logs --tail=20 worker-deep worker-action
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('SELECT lane, state, COUNT(*) FROM queue_jobs GROUP BY lane, state')
for row in c.fetchall():
    print(row)
"
```

---

## Focused Soak

Run the focused soak gate after at least one full triage + alert cycle:

```bash
python scripts/focused_soak_gate.py --mode soak --json
```

The 8 check names (stable, referenced in alerts and runbooks):

1. `old_services_stopped` — all legacy units inactive
2. `redis_owned_and_configured` — Redis ping + `appendonly yes`
3. `sources_fresh` — each enabled source polled within `IIC_SOURCE_STALE_AFTER_SECONDS`
4. `deferred_retry_bounded` — pending retry rows ≤ threshold, no orphaned events
5. `llm_calls_present` — at least one classification call recorded
6. `llm_failures_bounded` — no parse failures and no transport failures
7. `no_unexpected_api_classification_spend` — zero API-provider cost unless `allow_api_classification_spend=true`
8. `delivery_groups_bounded` — failed delivery groups ≤ threshold

In `--mode preflight`, checks `sources_fresh` and `llm_calls_present` are
skipped (marked PASS with a detail note) because a freshly started stack has
not yet produced evidence. All other checks run in both modes.

Preflight gate (before first triage cycle):

```bash
python scripts/focused_soak_gate.py --mode preflight --json
```

---

## Backup

```bash
bash ops/backup.sh
```

The backup script saves Redis RDB via `docker compose exec redis redis-cli SAVE`
then copies from the `iic-forge_iic_redis_data` volume, plus the SQLite data
directory.

---

## Rollback

1. Stop the Compose runtime:

```bash
docker compose down
```

2. Restore the previous app image or branch:

```bash
git checkout <previous-sha>
# or: docker pull iic-forge:<previous-tag>
```

3. Restore SQLite and Redis data from `ops/backup.sh` output if the rollback
   requires prior state:

```bash
# SQLite: copy saved data/ back to /srv/iic-forge/data/
# Redis: copy redis-dump.rdb into the volume and restart
docker run --rm \
  -v iic-forge_iic_redis_data:/data \
  -v /srv/iic-forge/backups/<stamp>:/backup:ro \
  alpine:3.20 \
  sh -c 'cp /backup/redis-dump.rdb /data/dump.rdb'
```

4. Restart Compose from the restored revision:

```bash
docker compose --profile runtime --profile sources --profile dashboard up -d
```

5. Run preflight again to confirm the rolled-back state is clean:

```bash
python scripts/focused_soak_gate.py --mode preflight --json
```

---

## Environment Template Reference

Key variables in `ops/env.iic-forge.example`:

| Variable | Purpose |
|---|---|
| `TRADINGAGENTS_IIC_DB_PATH` | SQLite path inside container (`/data/iic.db`) |
| `TRADINGAGENTS_SENSING_REDIS_URL` | Redis URL for adapters and triage (`redis://redis:6379/0`) |
| `LOCAL_LLM_BASE_URL` | Local model server base URL |
| `IIC_TRIAGE_LLM_PROVIDER` / `IIC_ALERT_GATE_LLM_PROVIDER` | LLM provider (`local` or `openai`) |
| `IIC_DELIVERY_POLICY` | Delivery policy (`ordered_telegram_email`) |
| `IIC_WORKER_DEEP_CONCURRENCY` | Deep-analysis worker slots (default 1) |
| `IIC_SOURCE_STALE_AFTER_SECONDS` | Freshness threshold for gate check (default 1800) |
| `IIC_DEFERRED_RETRY_MAX_PENDING` | Gate threshold for pending retries (default 0) |
| `DASHBOARD_PORT` | Streamlit dashboard host port (default 8501) |

Never edit `ops/env.iic-forge.example` in place. The example file is
contract-tested and committed to the repository.
