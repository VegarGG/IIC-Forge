# IIC-Forge Operating Guide

A single operator-facing runbook for running IIC-Forge day to day: what the
system is, what runs, how to start/stop it, how to drive the approval workflow,
how to watch it, and how to fix it when something breaks.

This guide ties together the focused runbooks already in this directory — treat
those as the authoritative deep-dives and this as the map:

- **[`service-platform.md`](service-platform.md)** — canonical Compose launch,
  cutover, rollback, backup, focused soak.
- **[`local-llm.md`](local-llm.md)** — running triage/promoter against a local
  llama-server, model swaps, fallback policy.
- **[`f3-exit-gate.md`](f3-exit-gate.md) / [`f4-exit-gate.md`](f4-exit-gate.md)
  / [`f5-exit-gate.md`](f5-exit-gate.md)** — per-phase acceptance gates.

All commands assume the repo root as the working directory
(`/opt/iic-forge` on the production host).

---

## 1. What the system is

IIC-Forge is an **always-on, local-first investment-intelligence desk**. It
senses the market 24/7, triages what matters, and — **only on your approval** —
runs a full multi-agent TradingAgents study and delivers a brief. It is
**decision-support only; it never places orders.**

The defining design choice is the **approval gate**: an event alert is *light
and cheap*. The expensive study runs only after you say yes.

```
SENSE (F3)          TRIAGE (F3)            ORCHESTRATOR (F4)        SECRETARY (F1)
adapters ─┐                                promoter polls events    stateful service:
polygon   ├─► redis ─► triage ─► events ─► salient + watchlist ──►  • composes LIGHT alert
rss       │   stream   dedupe   (sqlite)   + high-confidence         • one pending action
gdelt     │            salience                    │ (light alert,     per affected ticker
macro     │            ticker-tag                  │  no study yet)   • on approval →
telegram ─┘            watchlist                    ▼                   enqueues heavy study
                       auto-promote      ┌────────────────────────┐           │
 on-demand CLI ──────────────────────►  │  approve? (you decide)  │           ▼
 morning timer ──────────────────────►  │  telegram buttons / CLI │   TradingAgents graph
                                         └───────────┬────────────┘   (balanced IIC persona)
                                                     │ approved               │
                                       worker leases job ───────►   STATE (SQLite WAL + fs)
                                       runs the full study         runs, briefs, events,
                                                     │             watchlist, actions
              DELIVERY (F5) ◄──────────────────────-┴───────────────────────-┘
              telegram / email / cli  +  Streamlit dashboard
```

**System of record:** SQLite at `/data/iic.db` (WAL mode), bind-mounted to the
host at `/srv/iic-forge/data/iic.db`. Redis is **only** the ingest stream +
dedupe fingerprint cache — it is not the source of truth. Long-form analyst
reports live on disk under `data/runs/<run_id>/`, referenced by path from
SQLite.

### Three operating modes

1. **Event-triggered alerts** — triage flags a watchlist-relevant,
   high-confidence event → you get a terse light alert → heavy study runs only
   after you approve.
2. **Morning digest** — a scheduled daily brief over the watchlist.
3. **On-demand deep-dive** — you pick a ticker, you get a full brief
   synchronously (no daemons needed).

---

## 2. What runs (process inventory)

The canonical runtime is **Docker Compose** (`compose.yml`). Services are grouped
by profile so you start only what you need.

| Service | Profile | Entrypoint | Role | Notes |
|---|---|---|---|---|
| `redis` | (always) | redis-server | ingest stream + dedupe cache | AOF on; owns `iic_redis_data` volume |
| `adapter-polygon` | `sources` | `sensing.adapters.polygon_news` | Polygon news ingest | needs `POLYGON_API_KEY` |
| `adapter-rss` | `sources` | `sensing.adapters.rss` | RSS ingest | needs `RSS_FEEDS` |
| `adapter-gdelt` | `sources` | `sensing.adapters.gdelt` | GDELT DOC ingest | `GDELT_QUERY` must wrap OR-lists in `()` |
| `adapter-macro` | `sources` | `sensing.adapters.macro` | FRED macro releases | needs `FRED_API_KEY` or emits nothing |
| `adapter-telegram` | `sources` | `sensing.adapters.telegram` | Telegram channel ingest | session account must **join** channels |
| `adapter-x` | `x` | `sensing.adapters.x` | X/Twitter ingest | **off by default**; needs `X_BEARER_TOKEN` |
| `triage` | `runtime` | `sensing.triage` | dedupe → salience → ticker-tag → watchlist | calls the **local LLM** (`triage_salience` role) |
| `promoter` | `runtime` | `orchestrator.promoter` | promote events → light alert + pending actions | calls the **local LLM** (`alert_gate` role) |
| `worker-deep` | `runtime` | `orchestrator.worker` (`LANE=deep`) | runs full TradingAgents studies | effective concurrency 1 |
| `worker-action` | `runtime` | `orchestrator.worker` (`LANE=action`) | action-lane jobs | **idle by design** — no producer routes here yet |
| `action-handler` | `runtime` | `cli.main forge action-handler run` | turns approvals into study jobs | the approval→study bridge |
| `delivery` | `runtime`,`delivery` | `delivery.telegram_bot` | Telegram approval buttons + alert delivery | needs bot token + allowed chat ids |
| `dashboard` | `dashboard` | streamlit | Streamlit ops panel on port 8501 | only profile that publishes a host port |
| `gate-runner` | `gate` | `scripts/focused_soak_gate.py` | one-shot soak gate inside the stack | for CI/containerized gating |

**Not a Compose service:** the **local LLM server** (llama.cpp / Ollama / LM
Studio) runs on the host, outside Compose. Containers reach it via
`host.docker.internal` (already wired through `extra_hosts: host-gateway`).

> The `ops/systemd/*` units are the **decommissioned legacy** host deployment.
> Do not start them on a Compose host — the focused soak gate's
> `old_services_stopped` check will fail. They are retained for reference only.

---

## 3. Configuration

Config lives in `tradingagents/default_config.py` (env-overridable). Secrets and
operator overrides live in `.env` (never committed).

Compose layers two env files: `ops/env.iic-forge.example` (committed template,
contract-tested — **never edit in place**) then `.env` (your private overrides,
wins on conflict).

> **Launch step is always:** `cp ops/env.iic-forge.example .env` then edit the
> copy.

### Essential variables

| Variable | Purpose |
|---|---|
| `TRADINGAGENTS_IIC_DB_PATH` | SQLite path inside container (`/data/iic.db`) |
| `TRADINGAGENTS_SENSING_REDIS_URL` | Redis URL for adapters + triage (`redis://redis:6379/0`) |
| `LOCAL_LLM_BASE_URL` | Local model server URL (`http://host.docker.internal:8080/v1`) |
| `IIC_TRIAGE_LLM_PROVIDER` / `_MODEL` | triage `salience` role (`local` + model id) |
| `IIC_ALERT_GATE_LLM_PROVIDER` / `_MODEL` | promoter `alert_gate` role (`local` + model id) |
| `IIC_LLM_FALLBACK_MODE` | `none` (recommended) or `api` — see local-llm runbook §3 |
| `DEEPSEEK_API_KEY` | cloud LLM for `worker-deep` full studies (and fallback path) |
| `POLYGON_API_KEY` | Polygon news adapter + ticker seed |
| `FRED_API_KEY` | macro adapter (no key → macro source emits nothing) |
| `RSS_FEEDS` | comma-separated RSS URLs |
| `GDELT_QUERY` | GDELT DOC query — wrap OR-lists in `()`, avoid `&` |
| `TELEGRAM_API_ID` / `_API_HASH` / `_SENSING_SESSION` / `_SENSING_CHANNELS` | Telegram sensing adapter |
| `IIC_TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_ALLOWED_CHAT_IDS` | approval/delivery bot |
| `IIC_SMTP_ENABLED` / `IIC_SMTP_USER` / `_APP_PASSWORD` / `_TO_ADDRS` | email fallback channel (opt-in, default off) |
| `DASHBOARD_PORT` | Streamlit host port (default 8501) |

### Behaviour tunables (gate + worker)

| Variable | Default | Meaning |
|---|---|---|
| `alert_approval_gate_enabled` | `True` | light→approve→study (vs. legacy auto-enqueue at `False`) |
| `alert_salience_threshold` / `alert_ticker_confidence_threshold` | `0.85` / `0.9` | how selective the alert trigger is |
| `alert_pending_ttl_hours` | `24` | how long a pending approval stays valid |
| `IIC_WORKER_DEEP_CONCURRENCY` | `1` | deep-study slots (concurrency >1 is future work) |
| `IIC_WORKER_JOB_TIMEOUT_MIN` | `20` | per-job timeout |
| `IIC_SOURCE_STALE_AFTER_SECONDS` | `1800` | source-freshness gate threshold |
| `IIC_DEFERRED_RETRY_MAX_PENDING` | `100` (template) / `0` (gate strict) | pending salience-retry ceiling |
| `IIC_DELIVERY_FAILED_GROUP_MAX` | `0` | max failed delivery groups before gate fails |
| `IIC_ALLOW_API_CLASSIFICATION_SPEND` | `false` | allow API-provider classification spend in the gate |
| cost/rate guards | `enabled=False` | coded but **off** through F0–F5 (measure first) |

---

## 4. Standard operating procedures

### 4.1 First launch (full procedure → `service-platform.md`)

```bash
cd /opt/iic-forge
sudo mkdir -p /srv/iic-forge/data /srv/iic-forge/backups   # bind-mount target MUST pre-exist
cp ops/env.iic-forge.example .env && $EDITOR .env

# Bring up the local LLM server on the host first (see local-llm.md §1 to probe it)

docker compose --profile runtime --profile sources --profile dashboard up -d
docker compose ps
docker compose logs --tail=50 triage promoter
```

Seed the ticker reference table once (≈12k US equities + crypto):

```bash
docker compose run --rm --entrypoint python triage -m cli.main forge sense reseed-tickers
```

Then run the **preflight gate** (see §6) before declaring the launch good.

### 4.2 Start / stop / restart

```bash
# Start everything
docker compose --profile runtime --profile sources --profile dashboard up -d

# Add optional profiles
docker compose --profile x up -d            # enable the X/Twitter adapter
docker compose --profile delivery up -d     # Telegram bot only

# Status & logs
docker compose ps
docker compose logs -f --tail=50 triage promoter worker-deep

# Restart a subset (e.g. after an .env LLM change — only the LLM consumers)
docker compose restart triage promoter

# Stop everything (graceful; workers honour stop signals mid-job)
docker compose down
```

### 4.3 The approval-gate workflow (the core daily loop)

When triage flags a watchlist-relevant, high-confidence event, the **promoter**
makes one cheap summary call and writes a light alert plus one pending
`run_full_study` action **per affected ticker** (and a same-day suppression so a
ticker alerts at most once per day). Nothing expensive has run yet.

**Approve via Telegram** (inline buttons `[Study NVDA] … [Study all] [Dismiss all]`)
or via CLI:

```bash
tradingagents forge alert list                  # pending light alerts (8-char brief ids)
tradingagents forge alert approve <brief-id>    # approve all tickers
tradingagents forge alert approve <brief-id> --ticker NVDA   # approve just one
tradingagents forge alert dismiss <brief-id>
```

On approval the **action-handler** enqueues the heavy `event_alert` job; the
**worker** runs the full balanced study and links the full brief back to the
light one via `parent_brief_id`. In Compose the action-handler runs as a service;
to run it as a one-shot consumer:

```bash
tradingagents forge action-handler run
```

> The F4 SLA is **alert latency** (event → light alert, p95 ≤ 5 min), not study
> throughput — studies fire at *your* approval rate.

### 4.4 On-demand deep-dive (no daemons required)

```bash
tradingagents deepdive AAPL          # full research brief with risk debate, synchronously
tradingagents analyze                # interactive analyst-selection flow
```

### 4.5 Morning digest

```bash
tradingagents forge morning-digest now    # compose + send the digest now
tradingagents forge digest tail           # show recent digests
```

(Under the legacy host deployment this was a 06:00 systemd timer; under Compose
schedule it with host cron or a timer that runs the command above.)

### 4.6 Watchlist management (the promotion gate)

Only watchlist instruments promote to alerts, so the watchlist *is* the alert
filter.

```bash
tradingagents forge watchlist add NVDA
tradingagents forge watchlist list
tradingagents forge watchlist remove NVDA
tradingagents forge sense sweep-watchlist     # one-shot TTL prune of expired auto-entries
```

### 4.7 Local LLM operation

Triage (`triage_salience`) and promoter (`alert_gate`) run against the host
local model; everything else uses the API provider. Full lifecycle — probes,
cutover, model swap, fallback, revert — is in **`local-llm.md`**. The two facts
to remember:

- After any local-LLM `.env` change: `docker compose restart triage promoter`.
- `IIC_LLM_FALLBACK_MODE=none` (default) means a dead local endpoint **refuses
  to start** and self-alerts — degrade loudly, not silently. Don't change this
  without raising `IIC_LLM_FALLBACK_DAILY_BUDGET` too.
- For early testing you can enable cloud fallback with an **isolated, removable**
  key (`IIC_LLM_FALLBACK_API_KEY`, separate from the workers' `DEEPSEEK_API_KEY`).
  See `local-llm.md` §3b for the recipe and the post-deployment teardown.

---

## 5. Monitoring

### 5.1 Dashboard (Streamlit, port 8501)

`http://127.0.0.1:8501` — five tabs:

| Tab | Shows |
|---|---|
| **Operational status** | sources freshness, deferred-retry backlog, orphaned events, oldest-pending age, failed delivery groups |
| **Recent briefs** | recent briefs + brief threads (light → full via `parent_brief_id`) |
| **Daily cost trend** | per-day LLM spend, local vs. API split |
| **Queue status** | `queue_jobs` by lane/state |
| **Brief actions** | pending/processed brief actions; follow-up composer |

If a host port isn't published, start the dashboard profile:
`docker compose --profile dashboard up -d`.

### 5.2 Queue, retry, and delivery (direct SQLite)

```bash
# Queue jobs by lane/state
sqlite3 /srv/iic-forge/data/iic.db \
  "SELECT lane, state, COUNT(*) FROM queue_jobs GROUP BY lane, state"

# Deferred salience retry health (want: pending below threshold, zero dead)
sqlite3 /srv/iic-forge/data/iic.db \
  "SELECT state, COUNT(*) FROM deferred_salience_retry GROUP BY state"

# Delivery chains (telegram rank 1 → email rank 2 fallback)
sqlite3 /srv/iic-forge/data/iic.db \
  "SELECT delivery_group_id, attempt_rank, channel, status, fallback_of
   FROM deliveries ORDER BY delivery_group_id, attempt_rank LIMIT 50"
```

Also: `tradingagents forge orchestrator status` for queue + recent jobs.

### 5.3 LLM / fallback counters

```bash
sqlite3 /srv/iic-forge/data/iic.db \
  "SELECT name, value FROM ops_counters
   WHERE name LIKE '%llm%' OR name LIKE '%fallback%' ORDER BY name"
```

Key counters: `triage_llm_failures`, `promoter_llm_failures`,
`{triage,promoter}_fallback_calls:<YYYY-MM-DD>`.

---

## 6. Health & soak gates

The focused soak gate is the single launch/health acceptance check.

```bash
# Always source the operator env first so template thresholds load; the explicit
# DB path override AFTER `set +a` wins over the container path in .env.
set -a; . ./.env; set +a

# Before the first triage cycle:
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode preflight --json

# After ≥1 full triage + alert cycle:
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode soak --json
```

**Pass = exit code 0 and `"pass": true`.** The 8 stable checks:

1. `old_services_stopped` — all 17 legacy systemd units inactive
2. `redis_owned_and_configured` — Redis ping + `appendonly yes`
3. `sources_fresh` — each enabled source polled within `IIC_SOURCE_STALE_AFTER_SECONDS` *(skipped in preflight)*
4. `deferred_retry_bounded` — pending retries ≤ threshold, no orphaned events
5. `llm_calls_present` — ≥1 classification call recorded *(skipped in preflight)*
6. `llm_failures_bounded` — no parse and no transport failures
7. `no_unexpected_api_classification_spend` — zero API classification cost unless explicitly allowed
8. `delivery_groups_bounded` — failed delivery groups ≤ threshold

Phase-level acceptance lives in `scripts/f3_exit_gate.py`,
`scripts/f4_f5_exit_gate.py` (combined approval-through-delivery), and the
`f*-exit-gate.md` runbooks. Local-model soak counters:
`python scripts/f4_f5_exit_gate.py --soak-report [--local-model-id <id>] [--json]`.

---

## 7. Backup, restore, rollback

### Backup

```bash
bash ops/backup.sh        # → /srv/iic-forge/backups/<UTC-stamp>/
```

Writes a Redis RDB snapshot (`redis-cli SAVE` → copy `dump.rdb`) and a
consistent SQLite `.backup` (`iic.db`). Schedule it with host cron.

### Restore / rollback (full procedure → `service-platform.md` §Rollback)

```bash
docker compose down
git checkout <previous-sha>

# SQLite — restore the file directly (stack must be stopped):
cp /srv/iic-forge/backups/<stamp>/iic.db /srv/iic-forge/data/iic.db

# Redis — copy dump back into the volume:
docker run --rm \
  -v iic-forge_iic_redis_data:/data \
  -v /srv/iic-forge/backups/<stamp>:/backup:ro \
  alpine:3.20 sh -c 'cp /backup/redis-dump.rdb /data/dump.rdb'

docker compose --profile runtime --profile sources --profile dashboard up -d --build
# Re-run preflight to confirm the rolled-back state is clean (§6)
```

Because everything is resumable (sensing cursors, job leases, idempotent
writes), a restart never duplicates work and the worker honours stop signals
mid-job.

---

## 8. Troubleshooting playbook

| Symptom | Likely cause | Diagnose / fix |
|---|---|---|
| `triage`/`promoter` won't start, logs show probe failure | local LLM endpoint down (`fallback: none` refuses to start — by design) | Probe it (`local-llm.md` §1): `curl -fs $LOCAL_LLM_BASE_URL/../health`; start/recover llama-server; `docker compose restart triage promoter` |
| Gate `old_services_stopped` FAIL | a legacy systemd unit is still active | `sudo systemctl disable --now iic-*.service iic-*.timer redis-server.service` (full list in `service-platform.md`) |
| Gate `sources_fresh` FAIL | an adapter stalled or is missing its key/feed | `docker compose logs --tail=50 adapter-<name>`; confirm the source's API key / `RSS_FEEDS` / `GDELT_QUERY` is set; restart the adapter |
| Macro adapter silent | `FRED_API_KEY` unset | set it in `.env`, `docker compose restart adapter-macro` |
| GDELT adapter errors | `GDELT_QUERY` has bare OR-list or `&` | wrap OR-lists in `()`, e.g. `'(earnings OR "Federal Reserve")'` |
| Telegram sensing returns nothing | session account hasn't **joined** the channels | join the channels with the session account; verify `TELEGRAM_SENSING_CHANNELS` |
| Gate `deferred_retry_bounded` FAIL with `orphaned=<n>` | events with no salience outcome (triage LLM failing) | check `deferred_salience_retry` table + `triage_llm_failures`; restore the LLM endpoint; retries drain on the next poll |
| Gate `llm_calls_present` FAIL after launch | no triage cycle has run yet, or LLM never reached | wait one poll cycle; if persistent, treat as endpoint-down |
| Gate `delivery_groups_bounded` FAIL | a delivery group has a failed attempt and zero sent | inspect `deliveries`; check Telegram bot token / chat ids; enable SMTP fallback (`IIC_SMTP_ENABLED=true`) if Telegram is unreliable |
| Approved alert never produces a study | `action-handler` not consuming | confirm the `action-handler` service is up (`docker compose ps`) or run `tradingagents forge action-handler run` |
| `worker-action` shows no jobs | **expected** — no producer routes `lane=action` yet (idle by design) | none |
| API spend appears during local-mode soak | fallback flipped to `api`, or an adapter using API classification | check `ops_counters` `*_fallback_calls`; keep `IIC_LLM_FALLBACK_MODE=none` unless intended |
| Host gate/sqlite3 can't see container data | bind dir missing or wrong path | files are at `/srv/iic-forge/data`; ensure it pre-existed before `up` |
| Self-alert "local LLM endpoint down" | ≥`fallback_threshold` (3) consecutive LLM failures | one alert per outage (debounced); re-arms on recovery. Logs: `docker compose logs triage promoter | grep SELF-ALERT` |

---

## 9. Quick reference

```bash
# ── Lifecycle ──────────────────────────────────────────────
docker compose --profile runtime --profile sources --profile dashboard up -d
docker compose ps
docker compose logs -f --tail=50 triage promoter worker-deep
docker compose restart triage promoter        # after a local-LLM .env change
docker compose down

# ── Approval loop ──────────────────────────────────────────
tradingagents forge alert list
tradingagents forge alert approve <brief-id> [--ticker NVDA]
tradingagents forge alert dismiss <brief-id>
tradingagents forge action-handler run

# ── On-demand / digest ─────────────────────────────────────
tradingagents deepdive AAPL
tradingagents forge morning-digest now
tradingagents forge digest tail

# ── Watchlist & sensing ────────────────────────────────────
tradingagents forge watchlist add|list|remove <TICKER>
tradingagents forge sense reseed-tickers [--no-polygon]
tradingagents forge sense sweep-watchlist

# ── Orchestrator ───────────────────────────────────────────
tradingagents forge orchestrator status

# ── Health, backup ─────────────────────────────────────────
set -a; . ./.env; set +a
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode soak --json
bash ops/backup.sh

# ── Dashboard ──────────────────────────────────────────────
# http://127.0.0.1:8501   (docker compose --profile dashboard up -d)
```

Any `tradingagents forge ...` command also runs as
`python -m cli.main forge ...`, and inside the stack as
`docker compose run --rm --entrypoint python <service> -m cli.main forge ...`.
