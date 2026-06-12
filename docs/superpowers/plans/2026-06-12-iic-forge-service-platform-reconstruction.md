# IIC-Forge Service Platform Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the canonical IIC-Forge fork into a production-owned service platform with Compose-owned Redis/runtime services, shared operational ledgers, durable deferred salience retry, ordered Telegram/email delivery, and dashboard/gate evidence for launch readiness.

**Architecture:** Keep the existing TradingAgents graph, Secretary workflows, prompts, personas, local-model role routing, and investment behavior intact. Reconstruct the runtime boundary around Docker Compose, append-only SQLite control-plane tables, and small service modules that make state transfer explicit: adapters write health, triage writes LLM/deferred evidence, promoter/workers/delivery write auditable outcomes, and the dashboard plus focused soak gate read the same state.

**Tech Stack:** Python 3.10+, SQLite/WAL (`tradingagents.persistence`), Redis Streams, Docker Compose, Streamlit, Typer, pytest (`@pytest.mark.unit`), PyYAML, existing LangChain/OpenAI-compatible LLM clients.

**Spec:** `docs/superpowers/specs/2026-06-12-iic-forge-service-platform-reconstruction-design.md`

**Baseline:** The local-model reconstruction is already present in this branch: `local` provider support, `llm_roles`, local endpoint availability policy, `salience_source`, `ops_counters`, alert evaluator telemetry, and shadow eval support. This plan builds on those pieces; do not reimplement them unless a test below proves a regression.

---

## File Structure

**Create:**
- `compose.yml` - production runtime source of truth for the IIC-Forge Compose project.
- `ops/env.iic-forge.example` - IIC-specific env template for Compose/runtime settings.
- `ops/systemd/iic-forge-compose.service` - optional thin systemd supervisor for the whole Compose project.
- `ops/runbooks/service-platform.md` - launch, cutover, rollback, backup, and focused soak runbook.
- `tradingagents/llm_clients/ledger.py` - small helpers that normalize and persist `llm_calls` rows.
- `tradingagents/sensing/deferred_retry.py` - durable deferred-salience retry table helpers and retry runner.
- `tradingagents/sensing/source_health.py` - shared source liveness/cursor ledger helpers.
- `tradingagents/delivery/policy.py` - ordered Telegram-primary/email-fallback delivery policy.
- `tradingagents/dashboard/panels/operations.py` - dashboard query layer for the operational status tab.
- `scripts/focused_soak_gate.py` - focused production-readiness gate reading the shared evidence.
- `scripts/_repo_bootstrap.py` - subprocess-safe import bootstrap for direct `python scripts/*.py` execution.
- `tests/ops/test_compose_contract.py` - Compose/env/runtime contract tests.
- `tests/ops/test_runtime_path_contract.py` - production ops files must not point at old `TradingAgents` paths or `iic-redis`.
- `tests/persistence/test_platform_control_plane.py` - schema/helper tests for `llm_calls`, `source_health`, deferred retry, delivery chains, and queue lanes.
- `tests/llm_clients/test_llm_call_ledger.py` - ledger helper tests.
- `tests/sensing/test_deferred_salience_retry.py` - retry scheduling/backoff/claiming tests.
- `tests/sensing/test_source_health.py` - adapter health success/failure tests.
- `tests/delivery/test_ordered_policy.py` - Telegram primary/email fallback chain tests.
- `tests/orchestrator/test_worker_lanes.py` - lane-specific leasing and timeout evidence tests.
- `tests/dashboard/test_operations_panel.py` - dashboard operational query tests.
- `tests/scripts/test_focused_soak_gate.py` - focused soak gate tests.
- `tests/scripts/test_repo_bootstrap.py` - direct script import/entrypoint tests.

**Modify:**
- `Dockerfile` - keep image buildable for both CLI and module entrypoints; Compose can override `entrypoint`.
- `ops/backup.sh`, `ops/presoak.sh`, `ops/runbooks/f3-exit-gate.md`, `ops/runbooks/f4-exit-gate.md`, `ops/runbooks/local-llm.md`, `ops/systemd/redis-server.service` - remove production reliance on old paths/container names or mark legacy-only with a non-production warning.
- `tradingagents/persistence/schema.sql` - append `llm_calls`, `source_health`, `deferred_salience_retry`, delivery chain columns, and queue lane/timeout evidence columns.
- `tradingagents/persistence/store.py` - add insert/query helpers for new control-plane tables and delivery chains.
- `tradingagents/default_config.py` - add Compose/container-friendly env overrides, delivery ordered policy defaults, worker lane settings, source freshness thresholds, and focused soak thresholds.
- `tradingagents/sensing/adapters/base.py`, `tradingagents/sensing/adapters/gdelt.py`, `tradingagents/sensing/adapters/telegram.py`, plus `polygon_news.py`, `rss.py`, `macro.py`, `x.py` - wire source health updates.
- `tradingagents/sensing/triage.py` - record `llm_calls`, schedule durable deferred retry, and run due retries without depending on source republish.
- `tradingagents/orchestrator/alert_evaluator.py`, `tradingagents/orchestrator/promoter.py` - record gate/light-summary `llm_calls` and use ledger status normalization.
- `tradingagents/orchestrator/queue_store.py`, `tradingagents/orchestrator/worker.py`, `tradingagents/orchestrator/action_handler.py` - lane-aware queue leasing and timeout/backlog evidence.
- `tradingagents/secretary/service.py` - replace best-effort fan-out delivery loops with `delivery.policy.deliver_ordered`.
- `tradingagents/dashboard/app.py` - add an Operations tab that uses the same query helpers as the focused soak gate.
- `scripts/f4_f5_exit_gate.py`, `scripts/f5_exit_gate.py` - import via bootstrap and optionally include the new shared operational summary.

**Conventions:**
- Repo root for all commands: `/home/ziwei-huang/IIC-Forge/IIC-Forge`.
- Single test command shape: `python -m pytest <path>::<test_name> -v`.
- Use append-only SQLite migrations. `db.connect()` already tolerates duplicate `ALTER TABLE ADD COLUMN` errors.
- Tests that require live sockets, Docker, Redis, or local LLM endpoints must be marked `integration`; default unit tests use fakes and static contract checks.
- Commit after every green task. Do not commit unrelated dirty work.

---

## Phase 1 - Canonical Runtime Foundation

### Task 1: Compose stack and env template contract

**Files:**
- Create: `compose.yml`
- Create: `ops/env.iic-forge.example`
- Create: `tests/ops/test_compose_contract.py`
- Modify: `Dockerfile`

- [ ] **Step 1: Write the failing contract tests**

Create `tests/ops/test_compose_contract.py`:

```python
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_compose_defines_iic_owned_runtime_services():
    data = yaml.safe_load((ROOT / "compose.yml").read_text())
    assert data["name"] == "iic-forge"
    services = data["services"]
    expected = {
        "redis",
        "adapter-polygon",
        "adapter-telegram",
        "adapter-x",
        "adapter-rss",
        "adapter-gdelt",
        "adapter-macro",
        "triage",
        "promoter",
        "worker-action",
        "worker-deep",
        "action-handler",
        "delivery",
        "dashboard",
        "gate-runner",
    }
    assert expected.issubset(services.keys())
    assert "iic-redis" not in "\n".join(services.keys())
    assert services["redis"]["image"].startswith("redis:7")
    assert "iic_redis_data:/data" in services["redis"]["volumes"]
    assert "./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro" in services["redis"]["volumes"]
    assert services["redis"]["command"] == ["redis-server", "/usr/local/etc/redis/redis.conf"]
    assert services["triage"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["promoter"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["dashboard"]["ports"] == ["${DASHBOARD_PORT:-8501}:8501"]
    assert "iic_redis_data" in data["volumes"]
    assert "iic_data" in data["volumes"]


@pytest.mark.unit
def test_compose_keeps_local_llm_external_and_configured_by_env():
    text = (ROOT / "compose.yml").read_text()
    assert "llama" not in text.lower()
    assert "LOCAL_LLM_BASE_URL" in text
    data = yaml.safe_load(text)
    for name in ("triage", "promoter"):
        service = data["services"][name]
        assert "ops/env.iic-forge.example" in service["env_file"]
        assert any("LOCAL_LLM_BASE_URL" in str(item) for item in service.get("environment", []))


@pytest.mark.unit
def test_env_template_covers_launch_configuration():
    text = (ROOT / "ops" / "env.iic-forge.example").read_text()
    required = [
        "TRADINGAGENTS_IIC_DB_PATH=/data/iic.db",
        "TRADINGAGENTS_IIC_DATA_DIR=/data",
        "TRADINGAGENTS_SENSING_REDIS_URL=redis://redis:6379/0",
        "LOCAL_LLM_BASE_URL=http://host.docker.internal:8080/v1",
        "IIC_TRIAGE_LLM_PROVIDER=local",
        "IIC_ALERT_GATE_LLM_PROVIDER=local",
        "IIC_DELIVERY_POLICY=ordered_telegram_email",
        "IIC_WORKER_DEEP_CONCURRENCY=1",
        "IIC_SOURCE_STALE_AFTER_SECONDS=1800",
    ]
    for needle in required:
        assert needle in text
    assert "TradingAgents/TradingAgents" not in text
    assert "iic-redis" not in text
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/ops/test_compose_contract.py -v
```

Expected: FAIL because `compose.yml` and `ops/env.iic-forge.example` do not exist.

- [ ] **Step 3: Add the Compose stack**

Create `compose.yml`:

```yaml
name: iic-forge

x-app: &app
  build:
    context: .
  image: iic-forge:local
  env_file:
    - ops/env.iic-forge.example
  volumes:
    - iic_data:/data
  restart: unless-stopped

x-redis-dep: &redis_dep
  redis:
    condition: service_healthy

services:
  redis:
    image: redis:7-alpine
    command: ["redis-server", "/usr/local/etc/redis/redis.conf"]
    volumes:
      - iic_redis_data:/data
      - ./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 10
    restart: unless-stopped

  adapter-polygon:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.adapters.polygon_news"]
    depends_on: *redis_dep
    profiles: ["sources"]

  adapter-telegram:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.adapters.telegram"]
    depends_on: *redis_dep
    profiles: ["sources"]

  adapter-x:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.adapters.x"]
    depends_on: *redis_dep
    profiles: ["sources", "x"]

  adapter-rss:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.adapters.rss"]
    depends_on: *redis_dep
    profiles: ["sources"]

  adapter-gdelt:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.adapters.gdelt"]
    depends_on: *redis_dep
    profiles: ["sources"]

  adapter-macro:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.adapters.macro"]
    depends_on: *redis_dep
    profiles: ["sources"]

  triage:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.sensing.triage"]
    depends_on: *redis_dep
    environment:
      - LOCAL_LLM_BASE_URL=${LOCAL_LLM_BASE_URL:-http://host.docker.internal:8080/v1}
    profiles: ["runtime"]

  promoter:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.orchestrator.promoter"]
    depends_on: *redis_dep
    environment:
      - LOCAL_LLM_BASE_URL=${LOCAL_LLM_BASE_URL:-http://host.docker.internal:8080/v1}
    profiles: ["runtime"]

  worker-action:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.orchestrator.worker"]
    depends_on: *redis_dep
    environment:
      - IIC_WORKER_LANE=action
      - IIC_WORKER_CONCURRENCY=${IIC_WORKER_ACTION_CONCURRENCY:-2}
    profiles: ["runtime"]

  worker-deep:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.orchestrator.worker"]
    depends_on: *redis_dep
    environment:
      - IIC_WORKER_LANE=deep
      - IIC_WORKER_CONCURRENCY=${IIC_WORKER_DEEP_CONCURRENCY:-1}
    profiles: ["runtime"]

  action-handler:
    <<: *app
    entrypoint: ["python", "-m", "cli.main", "forge", "action-handler", "run"]
    depends_on: *redis_dep
    profiles: ["runtime"]

  delivery:
    <<: *app
    entrypoint: ["python", "-m", "tradingagents.delivery.telegram_bot"]
    depends_on: *redis_dep
    profiles: ["runtime", "delivery"]

  dashboard:
    <<: *app
    entrypoint: ["streamlit", "run", "tradingagents/dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
    ports:
      - "${DASHBOARD_PORT:-8501}:8501"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"]
      interval: 30s
      timeout: 5s
      retries: 5
    profiles: ["dashboard"]

  gate-runner:
    <<: *app
    entrypoint: ["python", "scripts/focused_soak_gate.py"]
    depends_on: *redis_dep
    profiles: ["gate"]

volumes:
  iic_redis_data:
  iic_data:
```

Create `ops/env.iic-forge.example`:

```dotenv
# IIC-Forge production runtime template. Copy to a private env file before use.

TRADINGAGENTS_IIC_DB_PATH=/data/iic.db
TRADINGAGENTS_IIC_DATA_DIR=/data
TRADINGAGENTS_RESULTS_DIR=/data/logs
TRADINGAGENTS_CACHE_DIR=/data/cache
TRADINGAGENTS_MEMORY_LOG_PATH=/data/memory/trading_memory.md
TRADINGAGENTS_SENSING_REDIS_URL=redis://redis:6379/0

LOCAL_LLM_BASE_URL=http://host.docker.internal:8080/v1
LOCAL_LLM_API_KEY=
IIC_TRIAGE_LLM_PROVIDER=local
IIC_TRIAGE_LLM_MODEL=qwen3.6-27b-instruct-q4_k_m
IIC_ALERT_GATE_LLM_PROVIDER=local
IIC_ALERT_GATE_LLM_MODEL=qwen3.6-27b-instruct-q4_k_m
IIC_LLM_FALLBACK_MODE=none
IIC_LLM_FALLBACK_DAILY_BUDGET=0

IIC_DELIVERY_POLICY=ordered_telegram_email
TELEGRAM_BOT_ALLOWED_CHAT_IDS=
IIC_TELEGRAM_BOT_TOKEN=
IIC_SMTP_USER=
IIC_SMTP_APP_PASSWORD=
IIC_SMTP_TO_ADDRS=

POLYGON_API_KEY=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SENSING_SESSION=/data/telegram/iic_sensing.session
TELEGRAM_SENSING_CHANNELS=
GDELT_QUERY=(earnings OR "federal reserve" OR "mergers and acquisitions")
RSS_FEEDS=

IIC_WORKER_LANE=deep
IIC_WORKER_ACTION_CONCURRENCY=2
IIC_WORKER_DEEP_CONCURRENCY=1
IIC_WORKER_JOB_TIMEOUT_MIN=20
IIC_SOURCE_STALE_AFTER_SECONDS=1800
IIC_DEFERRED_RETRY_MAX_PENDING=100
IIC_DELIVERY_FAILED_GROUP_MAX=0
DASHBOARD_PORT=8501
```

Modify `Dockerfile` only if a direct `python -m ...` Compose entrypoint fails during execution. The intended current behavior is valid because Compose services override `entrypoint`.

- [ ] **Step 4: Run green**

Run:

```bash
python -m pytest tests/ops/test_compose_contract.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add compose.yml ops/env.iic-forge.example tests/ops/test_compose_contract.py Dockerfile
git commit -m "feat(ops): add canonical compose runtime contract"
```

### Task 2: Thin systemd supervisor and old runtime path cleanup

**Files:**
- Create: `ops/systemd/iic-forge-compose.service`
- Create: `tests/ops/test_runtime_path_contract.py`
- Modify: `ops/backup.sh`
- Modify: `ops/presoak.sh`
- Modify: `ops/systemd/redis-server.service`
- Modify: `ops/runbooks/f3-exit-gate.md`
- Modify: `ops/runbooks/f4-exit-gate.md`
- Modify: `ops/runbooks/local-llm.md`

- [ ] **Step 1: Write the failing path contract tests**

Create `tests/ops/test_runtime_path_contract.py`:

```python
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_FILES = [
    ROOT / "compose.yml",
    ROOT / "ops" / "env.iic-forge.example",
    ROOT / "ops" / "backup.sh",
    ROOT / "ops" / "presoak.sh",
    ROOT / "ops" / "systemd" / "iic-forge-compose.service",
    ROOT / "ops" / "systemd" / "redis-server.service",
    ROOT / "ops" / "runbooks" / "f3-exit-gate.md",
    ROOT / "ops" / "runbooks" / "f4-exit-gate.md",
    ROOT / "ops" / "runbooks" / "local-llm.md",
]


@pytest.mark.unit
def test_production_ops_files_do_not_reference_old_tree_or_redis_container():
    bad = {}
    for path in PRODUCTION_FILES:
        text = path.read_text()
        hits = [
            needle
            for needle in (
                "/home/ziwei-huang/TradingAgents/TradingAgents",
                "docker start iic-redis",
                "docker exec iic-redis",
                "docker stop iic-redis",
                "REDIS_CONTAINER=${REDIS_CONTAINER:-iic-redis}",
            )
            if needle in text
        ]
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert bad == {}


@pytest.mark.unit
def test_systemd_compose_supervisor_is_single_runtime_entrypoint():
    unit = (ROOT / "ops" / "systemd" / "iic-forge-compose.service").read_text()
    assert "docker compose" in unit
    assert "WorkingDirectory=/opt/iic-forge" in unit
    assert "ExecStart=/usr/bin/docker compose --profile runtime --profile sources --profile dashboard up" in unit
    assert "ExecStop=/usr/bin/docker compose down" in unit
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/ops/test_runtime_path_contract.py -v
```

Expected: FAIL because old paths and `iic-redis` still appear.

- [ ] **Step 3: Add the Compose supervisor**

Create `ops/systemd/iic-forge-compose.service`:

```ini
[Unit]
Description=IIC-Forge Compose runtime
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/iic-forge
Environment=COMPOSE_PROJECT_NAME=iic-forge
ExecStart=/usr/bin/docker compose --profile runtime --profile sources --profile dashboard up
ExecStop=/usr/bin/docker compose down
Restart=on-failure
RestartSec=10
TimeoutStartSec=0
TimeoutStopSec=120
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Replace old production path references**

Update `ops/backup.sh` so Redis backup reads the Compose volume rather than `iic-redis`:

```bash
#!/usr/bin/env bash
set -euo pipefail

COMPOSE_PROJECT=${COMPOSE_PROJECT:-iic-forge}
BACKUP_ROOT=${BACKUP_ROOT:-/srv/iic-forge/backups}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT_DIR="${BACKUP_ROOT}/${STAMP}"

mkdir -p "${OUT_DIR}"
docker compose -p "${COMPOSE_PROJECT}" exec -T redis redis-cli SAVE
docker run --rm \
  -v "${COMPOSE_PROJECT}_iic_redis_data:/redis:ro" \
  -v "${OUT_DIR}:/backup" \
  alpine:3.20 \
  sh -c 'cp /redis/dump.rdb /backup/redis-dump.rdb'
cp -a /srv/iic-forge/data "${OUT_DIR}/data"
echo "backup written to ${OUT_DIR}"
```

Update `ops/presoak.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO=${REPO:-/opt/iic-forge}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-iic-forge}

cd "${REPO}"
docker compose -p "${COMPOSE_PROJECT}" config >/tmp/iic-forge-compose-rendered.yml
docker compose -p "${COMPOSE_PROJECT}" up -d redis
docker compose -p "${COMPOSE_PROJECT}" exec -T redis redis-cli ping
python scripts/focused_soak_gate.py --mode preflight --json
```

Replace `ops/systemd/redis-server.service` with a non-production note:

```ini
[Unit]
Description=Legacy placeholder - Redis is owned by IIC-Forge Compose

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo "Redis is owned by compose.yml service redis; do not start a standalone legacy container."'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

In `ops/runbooks/f3-exit-gate.md`, `ops/runbooks/f4-exit-gate.md`, and `ops/runbooks/local-llm.md`, replace old `cd /home/ziwei-huang/TradingAgents/TradingAgents` examples with:

```bash
cd /opt/iic-forge
docker compose --profile runtime --profile sources --profile dashboard up -d
```

Replace `docker exec iic-redis ...` examples with:

```bash
docker compose exec redis redis-cli CONFIG GET appendonly
```

- [ ] **Step 5: Run green**

Run:

```bash
python -m pytest tests/ops/test_runtime_path_contract.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ops/systemd/iic-forge-compose.service tests/ops/test_runtime_path_contract.py ops/backup.sh ops/presoak.sh ops/systemd/redis-server.service ops/runbooks/f3-exit-gate.md ops/runbooks/f4-exit-gate.md ops/runbooks/local-llm.md
git commit -m "chore(ops): make compose the production runtime entrypoint"
```

---

## Phase 2 - Control-Plane Schema

### Task 3: Append platform control-plane tables and helpers

**Files:**
- Modify: `tradingagents/persistence/schema.sql`
- Modify: `tradingagents/persistence/store.py`
- Create: `tests/persistence/test_platform_control_plane.py`

- [ ] **Step 1: Write the failing schema/helper tests**

Create `tests/persistence/test_platform_control_plane.py`:

```python
import json

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
def test_record_llm_call_round_trip(conn):
    call_id = store.insert_llm_call(
        conn,
        created_ts="2026-06-12T10:00:00+00:00",
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        status="success",
        latency_ms=123,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=10,
        out_tokens=5,
        cache_hit_tokens=0,
        cache_miss_tokens=10,
        usd_estimate=0.0,
        error_class=None,
        error_message=None,
    )
    rows = store.fetch_llm_calls(conn, role="triage_salience")
    assert rows == [{
        "call_id": call_id,
        "created_ts": "2026-06-12T10:00:00+00:00",
        "role": "triage_salience",
        "service_name": "triage",
        "provider": "local",
        "model_id": "qwen3.6-27b-instruct-q4_k_m",
        "base_url": "http://host.docker.internal:8080/v1",
        "request_kind": "structured",
        "linked_type": "event",
        "linked_id": "ev1",
        "status": "success",
        "latency_ms": 123,
        "parse_ok": 1,
        "fallback_mode": "none",
        "fallback_used": 0,
        "in_tokens": 10,
        "out_tokens": 5,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 10,
        "usd_estimate": 0.0,
        "error_class": None,
        "error_message": None,
    }]


@pytest.mark.unit
def test_source_health_upsert_success_and_failure(conn):
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:00:00+00:00",
        last_success_ts="2026-06-12T10:00:01+00:00",
        last_event_ts="2026-06-12T10:00:02+00:00",
        cursor="20260612T100000Z",
        cursor_updated_ts="2026-06-12T10:00:03+00:00",
        events_emitted_last_poll=2,
        diagnostics={"quota": "ok"},
    )
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["events_emitted_total"] == 2
    assert row["events_emitted_last_poll"] == 2
    assert row["consecutive_failures"] == 0
    assert json.loads(row["diagnostics"]) == {"quota": "ok"}

    store.upsert_source_health_failure(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:05:00+00:00",
        error="HTTP 500",
        diagnostics={"url": "gdelt"},
    )
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["events_emitted_total"] == 2
    assert row["consecutive_failures"] == 1
    assert row["last_error"] == "HTTP 500"


@pytest.mark.unit
def test_deferred_salience_retry_lifecycle(conn):
    retry_id = store.insert_deferred_salience_retry(
        conn,
        event_id="ev-deferred",
        source="rss",
        raw_path="/data/events/staging/a.json",
        payload_hash="hash1",
        payload_json='{"source":"rss","text":"earnings shock"}',
        reason="llm_error",
        next_attempt_ts="2026-06-12T10:01:00+00:00",
    )
    due = store.claim_due_deferred_salience_retries(
        conn,
        now_ts="2026-06-12T10:02:00+00:00",
        limit=5,
    )
    assert [row["retry_id"] for row in due] == [retry_id]
    running = store.fetch_deferred_salience_retries(conn, state="running")
    assert running[0]["attempt_count"] == 1

    store.reschedule_deferred_salience_retry(
        conn,
        retry_id=retry_id,
        reason="parse_error",
        next_attempt_ts="2026-06-12T10:06:00+00:00",
    )
    pending = store.fetch_deferred_salience_retries(conn, state="pending")
    assert pending[0]["last_error"] == "parse_error"
    assert pending[0]["next_attempt_ts"] == "2026-06-12T10:06:00+00:00"

    store.mark_deferred_salience_retry_done(conn, retry_id=retry_id)
    done = store.fetch_deferred_salience_retries(conn, state="done")
    assert done[0]["retry_id"] == retry_id


@pytest.mark.unit
def test_delivery_chain_columns_round_trip(conn):
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert_light",
        scope='["NVDA"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    primary = store.insert_delivery(
        conn,
        brief_id="b1",
        channel="telegram",
        status="failed",
        sent_ts=None,
        channel_ref=None,
        skip_reason=None,
        delivery_group_id="grp1",
        attempt_rank=1,
        fallback_of=None,
        is_fallback=False,
        failure_reason="network",
    )
    fallback = store.insert_delivery(
        conn,
        brief_id="b1",
        channel="email",
        status="sent",
        sent_ts="2026-06-12T10:00:05+00:00",
        channel_ref="msg1",
        skip_reason=None,
        delivery_group_id="grp1",
        attempt_rank=2,
        fallback_of=primary,
        is_fallback=True,
        failure_reason=None,
    )
    chains = store.fetch_delivery_groups(conn)
    assert chains["grp1"][0]["delivery_id"] == primary
    assert chains["grp1"][1]["delivery_id"] == fallback
    assert chains["grp1"][1]["fallback_of"] == primary
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/persistence/test_platform_control_plane.py -v
```

Expected: FAIL because helpers/tables do not exist.

- [ ] **Step 3: Append schema**

Append to `tradingagents/persistence/schema.sql`:

```sql

-- ============================================================
-- Service platform reconstruction control plane
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts         TEXT NOT NULL,
    role               TEXT NOT NULL,
    service_name       TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    base_url           TEXT,
    request_kind       TEXT NOT NULL,
    linked_type        TEXT NOT NULL,
    linked_id          TEXT,
    status             TEXT NOT NULL,
    latency_ms         INTEGER,
    parse_ok           INTEGER,
    fallback_mode      TEXT,
    fallback_used      INTEGER NOT NULL DEFAULT 0,
    in_tokens          INTEGER,
    out_tokens         INTEGER,
    cache_hit_tokens   INTEGER,
    cache_miss_tokens  INTEGER,
    usd_estimate       REAL,
    error_class        TEXT,
    error_message      TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_role_ts ON llm_calls(role, created_ts);
CREATE INDEX IF NOT EXISTS idx_llm_calls_linked ON llm_calls(linked_type, linked_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_calls(status, created_ts);

CREATE TABLE IF NOT EXISTS source_health (
    source                    TEXT PRIMARY KEY,
    service_name              TEXT NOT NULL,
    last_poll_ts              TEXT,
    last_success_ts           TEXT,
    last_event_ts             TEXT,
    cursor                    TEXT,
    cursor_updated_ts         TEXT,
    events_emitted_total      INTEGER NOT NULL DEFAULT 0,
    events_emitted_last_poll  INTEGER NOT NULL DEFAULT 0,
    consecutive_failures      INTEGER NOT NULL DEFAULT 0,
    last_error                TEXT,
    last_error_ts             TEXT,
    diagnostics               TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_health_last_success ON source_health(last_success_ts);
CREATE INDEX IF NOT EXISTS idx_source_health_last_event ON source_health(last_event_ts);

CREATE TABLE IF NOT EXISTS deferred_salience_retry (
    retry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT,
    source            TEXT NOT NULL,
    raw_path          TEXT,
    payload_hash      TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    reason            TEXT NOT NULL,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts   TEXT NOT NULL,
    last_attempt_ts   TEXT,
    state             TEXT NOT NULL DEFAULT 'pending',
    last_error        TEXT,
    created_ts        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_ts        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_deferred_salience_retry_due
    ON deferred_salience_retry(state, next_attempt_ts);
CREATE INDEX IF NOT EXISTS idx_deferred_salience_retry_payload
    ON deferred_salience_retry(payload_hash, state);

ALTER TABLE deliveries ADD COLUMN delivery_group_id TEXT;
ALTER TABLE deliveries ADD COLUMN attempt_rank INTEGER;
ALTER TABLE deliveries ADD COLUMN fallback_of INTEGER REFERENCES deliveries(delivery_id);
ALTER TABLE deliveries ADD COLUMN is_fallback INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deliveries ADD COLUMN failure_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_deliveries_group_rank
    ON deliveries(delivery_group_id, attempt_rank);

ALTER TABLE queue_jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'deep';
ALTER TABLE queue_jobs ADD COLUMN heartbeat_ts TEXT;
ALTER TABLE queue_jobs ADD COLUMN timeout_seconds INTEGER;
CREATE INDEX IF NOT EXISTS idx_queue_jobs_lane_state
    ON queue_jobs(lane, state, enqueued_ts);
```

- [ ] **Step 4: Add store helpers**

Append to `tradingagents/persistence/store.py`:

```python

# --------------------------------------------------------------------
# Service platform reconstruction control-plane helpers
# --------------------------------------------------------------------

def _bool_to_int(value: Optional[bool]) -> Optional[int]:
    return None if value is None else (1 if value else 0)


def insert_llm_call(
    conn: sqlite3.Connection,
    *,
    created_ts: str,
    role: str,
    service_name: str,
    provider: str,
    model_id: str,
    base_url: Optional[str],
    request_kind: str,
    linked_type: str,
    linked_id: Optional[str],
    status: str,
    latency_ms: Optional[int],
    parse_ok: Optional[bool],
    fallback_mode: Optional[str],
    fallback_used: bool,
    in_tokens: Optional[int],
    out_tokens: Optional[int],
    cache_hit_tokens: Optional[int],
    cache_miss_tokens: Optional[int],
    usd_estimate: Optional[float],
    error_class: Optional[str],
    error_message: Optional[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO llm_calls (created_ts, role, service_name, provider, "
        "model_id, base_url, request_kind, linked_type, linked_id, status, "
        "latency_ms, parse_ok, fallback_mode, fallback_used, in_tokens, "
        "out_tokens, cache_hit_tokens, cache_miss_tokens, usd_estimate, "
        "error_class, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_ts, role, service_name, provider, model_id, base_url,
            request_kind, linked_type, linked_id, status, latency_ms,
            _bool_to_int(parse_ok), fallback_mode, 1 if fallback_used else 0,
            in_tokens, out_tokens, cache_hit_tokens, cache_miss_tokens,
            usd_estimate, error_class, (error_message[:1000] if error_message else None),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_llm_calls(conn: sqlite3.Connection, *, role: Optional[str] = None) -> list[dict]:
    if role is None:
        rows = conn.execute("SELECT * FROM llm_calls ORDER BY call_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM llm_calls WHERE role = ? ORDER BY call_id",
            (role,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_source_health_success(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    last_poll_ts: str,
    last_success_ts: str,
    last_event_ts: Optional[str],
    cursor: Optional[str],
    cursor_updated_ts: Optional[str],
    events_emitted_last_poll: int,
    diagnostics: Optional[dict] = None,
) -> None:
    conn.execute(
        "INSERT INTO source_health (source, service_name, last_poll_ts, "
        "last_success_ts, last_event_ts, cursor, cursor_updated_ts, "
        "events_emitted_total, events_emitted_last_poll, consecutive_failures, "
        "last_error, last_error_ts, diagnostics) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "service_name = excluded.service_name, "
        "last_poll_ts = excluded.last_poll_ts, "
        "last_success_ts = excluded.last_success_ts, "
        "last_event_ts = COALESCE(excluded.last_event_ts, source_health.last_event_ts), "
        "cursor = COALESCE(excluded.cursor, source_health.cursor), "
        "cursor_updated_ts = COALESCE(excluded.cursor_updated_ts, source_health.cursor_updated_ts), "
        "events_emitted_total = source_health.events_emitted_total + excluded.events_emitted_last_poll, "
        "events_emitted_last_poll = excluded.events_emitted_last_poll, "
        "consecutive_failures = 0, "
        "last_error = NULL, "
        "last_error_ts = NULL, "
        "diagnostics = excluded.diagnostics",
        (
            source, service_name, last_poll_ts, last_success_ts, last_event_ts,
            cursor, cursor_updated_ts, events_emitted_last_poll,
            events_emitted_last_poll, json.dumps(diagnostics or {}),
        ),
    )
    conn.commit()


def upsert_source_health_failure(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    last_poll_ts: str,
    error: str,
    diagnostics: Optional[dict] = None,
) -> None:
    conn.execute(
        "INSERT INTO source_health (source, service_name, last_poll_ts, "
        "events_emitted_last_poll, consecutive_failures, last_error, "
        "last_error_ts, diagnostics) "
        "VALUES (?, ?, ?, 0, 1, ?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "service_name = excluded.service_name, "
        "last_poll_ts = excluded.last_poll_ts, "
        "events_emitted_last_poll = 0, "
        "consecutive_failures = source_health.consecutive_failures + 1, "
        "last_error = excluded.last_error, "
        "last_error_ts = excluded.last_error_ts, "
        "diagnostics = excluded.diagnostics",
        (
            source, service_name, last_poll_ts, error[:1000], last_poll_ts,
            json.dumps(diagnostics or {}),
        ),
    )
    conn.commit()


def fetch_source_health(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    return {r["source"]: dict(r) for r in rows}


def insert_deferred_salience_retry(
    conn: sqlite3.Connection,
    *,
    event_id: Optional[str],
    source: str,
    raw_path: Optional[str],
    payload_hash: str,
    payload_json: str,
    reason: str,
    next_attempt_ts: str,
) -> int:
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO deferred_salience_retry (event_id, source, raw_path, "
        "payload_hash, payload_json, reason, next_attempt_ts, state, "
        "last_error, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            event_id, source, raw_path, payload_hash, payload_json,
            reason[:500], next_attempt_ts, reason[:1000], now, now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def claim_due_deferred_salience_retries(
    conn: sqlite3.Connection,
    *,
    now_ts: str,
    limit: int,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM deferred_salience_retry "
        "WHERE state = 'pending' AND datetime(next_attempt_ts) <= datetime(?) "
        "ORDER BY next_attempt_ts, retry_id LIMIT ?",
        (now_ts, int(limit)),
    ).fetchall()
    ids = [r["retry_id"] for r in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE deferred_salience_retry SET state = 'running', "
            f"attempt_count = attempt_count + 1, last_attempt_ts = ?, updated_ts = ? "
            f"WHERE retry_id IN ({placeholders})",
            (now_ts, now_ts, *ids),
        )
        conn.commit()
    return [dict(r) for r in rows]


def reschedule_deferred_salience_retry(
    conn: sqlite3.Connection,
    *,
    retry_id: int,
    reason: str,
    next_attempt_ts: str,
) -> None:
    now = _now_iso()
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'pending', reason = ?, "
        "last_error = ?, next_attempt_ts = ?, updated_ts = ? WHERE retry_id = ?",
        (reason[:500], reason[:1000], next_attempt_ts, now, retry_id),
    )
    conn.commit()


def mark_deferred_salience_retry_done(conn: sqlite3.Connection, *, retry_id: int) -> None:
    now = _now_iso()
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'done', updated_ts = ? WHERE retry_id = ?",
        (now, retry_id),
    )
    conn.commit()


def mark_deferred_salience_retry_dead(
    conn: sqlite3.Connection,
    *,
    retry_id: int,
    reason: str,
) -> None:
    now = _now_iso()
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'dead', last_error = ?, updated_ts = ? "
        "WHERE retry_id = ?",
        (reason[:1000], now, retry_id),
    )
    conn.commit()


def fetch_deferred_salience_retries(
    conn: sqlite3.Connection,
    *,
    state: Optional[str] = None,
) -> list[dict]:
    if state is None:
        rows = conn.execute(
            "SELECT * FROM deferred_salience_retry ORDER BY retry_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deferred_salience_retry WHERE state = ? ORDER BY retry_id",
            (state,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_delivery_groups(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT * FROM deliveries WHERE delivery_group_id IS NOT NULL "
        "ORDER BY delivery_group_id, attempt_rank, delivery_id"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["delivery_group_id"], []).append(dict(row))
    return out
```

Update the existing `insert_delivery` signature in `store.py` by adding keyword-only defaults and the expanded insert:

```python
def insert_delivery(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    channel: str,
    status: str,
    sent_ts: Optional[str],
    channel_ref: Optional[str],
    skip_reason: Optional[str],
    delivery_group_id: Optional[str] = None,
    attempt_rank: Optional[int] = None,
    fallback_of: Optional[int] = None,
    is_fallback: bool = False,
    failure_reason: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO deliveries (brief_id, channel, status, sent_ts, channel_ref, "
        "skip_reason, delivery_group_id, attempt_rank, fallback_of, is_fallback, "
        "failure_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            brief_id, channel, status, sent_ts, channel_ref, skip_reason,
            delivery_group_id, attempt_rank, fallback_of, 1 if is_fallback else 0,
            (failure_reason[:1000] if failure_reason else None),
        ),
    )
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 5: Run green**

Run:

```bash
python -m pytest tests/persistence/test_platform_control_plane.py -v
python -m pytest tests/persistence -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/persistence/schema.sql tradingagents/persistence/store.py tests/persistence/test_platform_control_plane.py
git commit -m "feat(persistence): add service platform control plane"
```

---

## Phase 3 - Unified LLM Call Ledger

### Task 4: LLM ledger helper with normalized status

**Files:**
- Create: `tradingagents/llm_clients/ledger.py`
- Create: `tests/llm_clients/test_llm_call_ledger.py`
- Modify: `tradingagents/llm_clients/__init__.py`

- [ ] **Step 1: Write the failing ledger tests**

Create `tests/llm_clients/test_llm_call_ledger.py`:

```python
import pytest

from tradingagents.persistence.db import connect


@pytest.mark.unit
def test_record_llm_success_defaults_local_cost_to_zero(tmp_path):
    from tradingagents.llm_clients.ledger import record_llm_success
    from tradingagents.persistence import store

    conn = connect(str(tmp_path / "iic.db"))
    call_id = record_llm_success(
        conn,
        role="alert_gate",
        service_name="promoter",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        latency_ms=99,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        token_usage={"prompt_tokens": 12, "completion_tokens": 3},
    )
    row = store.fetch_llm_calls(conn)[0]
    assert row["call_id"] == call_id
    assert row["status"] == "success"
    assert row["usd_estimate"] == 0.0
    assert row["in_tokens"] == 12
    assert row["out_tokens"] == 3


@pytest.mark.unit
def test_record_llm_error_truncates_message_and_classifies_status(tmp_path):
    from tradingagents.llm_clients.ledger import record_llm_error
    from tradingagents.persistence import store

    conn = connect(str(tmp_path / "iic.db"))
    record_llm_error(
        conn,
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev2",
        status="parse_error",
        latency_ms=250,
        parse_ok=False,
        fallback_mode="none",
        fallback_used=False,
        exc=ValueError("x" * 2000),
    )
    row = store.fetch_llm_calls(conn)[0]
    assert row["status"] == "parse_error"
    assert row["error_class"] == "ValueError"
    assert len(row["error_message"]) == 1000
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/llm_clients/test_llm_call_ledger.py -v
```

Expected: FAIL because `ledger.py` does not exist.

- [ ] **Step 3: Implement ledger helper**

Create `tradingagents/llm_clients/ledger.py`:

```python
"""Unified LLM call ledger helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from tradingagents.persistence import store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(token_usage: Optional[dict[str, Any]]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    if not token_usage:
        return None, None, None, None
    in_tokens = token_usage.get("prompt_tokens") or token_usage.get("input_tokens")
    out_tokens = token_usage.get("completion_tokens") or token_usage.get("output_tokens")
    cache_hit = token_usage.get("prompt_cache_hit_tokens") or token_usage.get("cache_hit_tokens")
    cache_miss = token_usage.get("prompt_cache_miss_tokens") or token_usage.get("cache_miss_tokens")
    return (
        int(in_tokens) if in_tokens is not None else None,
        int(out_tokens) if out_tokens is not None else None,
        int(cache_hit) if cache_hit is not None else None,
        int(cache_miss) if cache_miss is not None else None,
    )


def _usd(provider: str, explicit: Optional[float]) -> Optional[float]:
    if explicit is not None:
        return float(explicit)
    return 0.0 if provider == "local" else None


def record_llm_success(
    conn: sqlite3.Connection,
    *,
    role: str,
    service_name: str,
    provider: str,
    model_id: str,
    base_url: Optional[str],
    request_kind: str,
    linked_type: str,
    linked_id: Optional[str],
    latency_ms: Optional[int],
    parse_ok: Optional[bool],
    fallback_mode: Optional[str],
    fallback_used: bool,
    token_usage: Optional[dict[str, Any]] = None,
    usd_estimate: Optional[float] = None,
) -> int:
    in_tokens, out_tokens, cache_hit, cache_miss = _tokens(token_usage)
    return store.insert_llm_call(
        conn,
        created_ts=_now_iso(),
        role=role,
        service_name=service_name,
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        request_kind=request_kind,
        linked_type=linked_type,
        linked_id=linked_id,
        status="success",
        latency_ms=latency_ms,
        parse_ok=parse_ok,
        fallback_mode=fallback_mode,
        fallback_used=fallback_used,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        usd_estimate=_usd(provider, usd_estimate),
        error_class=None,
        error_message=None,
    )


def record_llm_error(
    conn: sqlite3.Connection,
    *,
    role: str,
    service_name: str,
    provider: str,
    model_id: str,
    base_url: Optional[str],
    request_kind: str,
    linked_type: str,
    linked_id: Optional[str],
    status: str,
    latency_ms: Optional[int],
    parse_ok: Optional[bool],
    fallback_mode: Optional[str],
    fallback_used: bool,
    exc: BaseException,
    usd_estimate: Optional[float] = None,
) -> int:
    return store.insert_llm_call(
        conn,
        created_ts=_now_iso(),
        role=role,
        service_name=service_name,
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        request_kind=request_kind,
        linked_type=linked_type,
        linked_id=linked_id,
        status=status,
        latency_ms=latency_ms,
        parse_ok=parse_ok,
        fallback_mode=fallback_mode,
        fallback_used=fallback_used,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=_usd(provider, usd_estimate),
        error_class=type(exc).__name__,
        error_message=str(exc)[:1000],
    )
```

Export the helper names in `tradingagents/llm_clients/__init__.py`:

```python
from .ledger import record_llm_error, record_llm_success
```

- [ ] **Step 4: Run green**

Run:

```bash
python -m pytest tests/llm_clients/test_llm_call_ledger.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/llm_clients/ledger.py tradingagents/llm_clients/__init__.py tests/llm_clients/test_llm_call_ledger.py
git commit -m "feat(llm): add unified call ledger helper"
```

### Task 5: Wire triage, promoter, and light summary calls into `llm_calls`

**Files:**
- Modify: `tradingagents/sensing/salience.py`
- Modify: `tradingagents/sensing/triage.py`
- Modify: `tradingagents/orchestrator/alert_evaluator.py`
- Modify: `tradingagents/orchestrator/promoter.py`
- Modify: `tradingagents/secretary/service.py`
- Create: `tests/llm_clients/test_llm_call_wiring.py`

- [ ] **Step 1: Write the failing wiring tests**

Create `tests/llm_clients/test_llm_call_wiring.py`:

```python
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_alert_gate_records_llm_call_on_success(tmp_path):
    from tradingagents.orchestrator.alert_evaluator import record_alert_gate_llm_call

    conn = connect(str(tmp_path / "iic.db"))
    store.insert_event(
        conn,
        event_id="ev1",
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        salience=0.9,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="llm",
    )
    record_alert_gate_llm_call(
        conn,
        event_id="ev1",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        latency_ms=111,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
    )
    row = store.fetch_llm_calls(conn, role="alert_gate")[0]
    assert row["service_name"] == "promoter"
    assert row["linked_type"] == "event"
    assert row["linked_id"] == "ev1"
    assert row["status"] == "success"


@pytest.mark.unit
def test_light_summary_records_llm_call(tmp_path):
    from tradingagents.secretary.service import record_light_summary_llm_call

    conn = connect(str(tmp_path / "iic.db"))
    record_light_summary_llm_call(
        conn,
        brief_id="brief1",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        latency_ms=88,
        fallback_mode="none",
        fallback_used=False,
    )
    row = store.fetch_llm_calls(conn, role="light_alert_summary")[0]
    assert row["linked_type"] == "brief"
    assert row["linked_id"] == "brief1"
    assert row["usd_estimate"] == 0.0
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/llm_clients/test_llm_call_wiring.py -v
```

Expected: FAIL because helper seams do not exist.

- [ ] **Step 3: Add small record helper seams**

In `tradingagents/orchestrator/alert_evaluator.py`, add:

```python
def record_alert_gate_llm_call(
    conn,
    *,
    event_id: str,
    provider: str,
    model_id: str,
    base_url: str | None,
    latency_ms: int | None,
    parse_ok: bool | None,
    fallback_mode: str | None,
    fallback_used: bool,
) -> int:
    from tradingagents.llm_clients.ledger import record_llm_success

    return record_llm_success(
        conn,
        role="alert_gate",
        service_name="promoter",
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        request_kind="structured",
        linked_type="event",
        linked_id=event_id,
        latency_ms=latency_ms,
        parse_ok=parse_ok,
        fallback_mode=fallback_mode,
        fallback_used=fallback_used,
    )
```

In the existing alert evaluation call path, call this helper immediately after `insert_alert_evaluation(...)` using the same measured `model_id`, `parse_ok`, and `latency_ms` values already persisted to `alert_evaluations`. Resolve `provider`, `base_url`, `fallback_mode`, and `fallback_used` from the role client/fallback state already built in `promoter.main`.

In `tradingagents/secretary/service.py`, add:

```python
def record_light_summary_llm_call(
    conn,
    *,
    brief_id: str,
    provider: str,
    model_id: str,
    base_url: str | None,
    latency_ms: int | None,
    fallback_mode: str | None,
    fallback_used: bool,
) -> int:
    from tradingagents.llm_clients.ledger import record_llm_success

    return record_llm_success(
        conn,
        role="light_alert_summary",
        service_name="promoter",
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        request_kind="chat",
        linked_type="brief",
        linked_id=brief_id,
        latency_ms=latency_ms,
        parse_ok=True,
        fallback_mode=fallback_mode,
        fallback_used=fallback_used,
    )
```

In `Secretary.compose_event_alert_light`, wrap `self._llm.invoke(prompt)` with `time.perf_counter()` and call `record_light_summary_llm_call` after `store.insert_brief(...)`. Use:

```python
provider = getattr(self._llm, "_iic_provider", "unknown")
model_id = getattr(self._llm, "model_name", None) or getattr(self._llm, "model", "unknown")
base_url = getattr(self._llm, "openai_api_base", None)
fallback_mode = getattr(self._llm, "_iic_fallback_mode", None)
fallback_used = bool(getattr(self._llm, "_iic_fallback_used", False))
```

In `tradingagents/sensing/triage.py`, after `score = await self._scorer.score(...)`, record:

```python
from tradingagents.llm_clients.ledger import record_llm_error, record_llm_success

if score.source == "llm":
    record_llm_success(
        self._conn,
        role="triage_salience",
        service_name="triage",
        provider=getattr(self._scorer, "provider", "unknown"),
        model_id=getattr(self._scorer, "model_id", "unknown"),
        base_url=getattr(self._scorer, "base_url", None),
        request_kind="structured",
        linked_type="event",
        linked_id=None,
        latency_ms=getattr(score, "latency_ms", None),
        parse_ok=True,
        fallback_mode=getattr(self._scorer, "fallback_mode", None),
        fallback_used=bool(getattr(self._scorer, "fallback_used", False)),
    )
elif score.source == "deferred":
    record_llm_error(
        self._conn,
        role="triage_salience",
        service_name="triage",
        provider=getattr(self._scorer, "provider", "unknown"),
        model_id=getattr(self._scorer, "model_id", "unknown"),
        base_url=getattr(self._scorer, "base_url", None),
        request_kind="structured",
        linked_type="event",
        linked_id=None,
        status=("parse_error" if "parse_error" in score.reason else "transport_error"),
        latency_ms=getattr(score, "latency_ms", None),
        parse_ok=False,
        fallback_mode=getattr(self._scorer, "fallback_mode", None),
        fallback_used=bool(getattr(self._scorer, "fallback_used", False)),
        exc=RuntimeError(score.reason),
    )
```

Before this snippet, add attributes to `SalienceScorer` in `tradingagents/sensing/salience.py` constructor with defaults:

```python
self.provider = "unknown"
self.model_id = "unknown"
self.base_url = None
self.fallback_mode = None
self.fallback_used = False
```

Then, in `triage._main`, after building `quick_client`, set:

```python
t._scorer.provider = quick_client.provider
t._scorer.model_id = quick_client.model
t._scorer.base_url = getattr(quick_client, "base_url", None)
t._scorer.fallback_mode = fallback_mode
t._scorer.fallback_used = used_fallback
```

- [ ] **Step 4: Run green**

Run:

```bash
python -m pytest tests/llm_clients/test_llm_call_wiring.py -v
python -m pytest tests/sensing/test_salience_schema_parse.py tests/orchestrator/test_alert_evaluator_telemetry.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/sensing/salience.py tradingagents/sensing/triage.py tradingagents/orchestrator/alert_evaluator.py tradingagents/orchestrator/promoter.py tradingagents/secretary/service.py tests/llm_clients/test_llm_call_wiring.py
git commit -m "feat(runtime): record classification and summary llm calls"
```

---

## Phase 4 - Durable Deferred Salience Retry

### Task 6: Deferred retry scheduler and runner

**Files:**
- Create: `tradingagents/sensing/deferred_retry.py`
- Create: `tests/sensing/test_deferred_salience_retry.py`
- Modify: `tradingagents/sensing/triage.py`

- [ ] **Step 1: Write the failing retry tests**

Create `tests/sensing/test_deferred_salience_retry.py`:

```python
import json

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store
from tradingagents.sensing.envelope import Envelope


@pytest.mark.unit
def test_schedule_deferred_retry_preserves_payload_and_backoff(tmp_path):
    from tradingagents.sensing.deferred_retry import schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:1",
        text="Company reports earnings shock",
        source_tags={"tickers": ["NVDA"]},
        raw_path="/data/events/staging/rss1.json",
    )
    retry_id = schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-deferred",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=60,
    )
    row = store.fetch_deferred_salience_retries(conn)[0]
    assert row["retry_id"] == retry_id
    assert row["source"] == "rss"
    assert row["raw_path"] == "/data/events/staging/rss1.json"
    assert row["payload_hash"]
    assert row["next_attempt_ts"] == "2026-06-12T10:01:00+00:00"
    payload = json.loads(row["payload_json"])
    assert payload["external_id"] == "rss:1"
    assert payload["source_tags"] == {"tickers": ["NVDA"]}


@pytest.mark.unit
async def test_retry_runner_marks_done_when_rescored_async(tmp_path):
    from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:1",
        text="Company reports earnings shock",
        source_tags={},
        raw_path="",
    )
    retry_id = schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-deferred",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=1,
    )

    class FakeTriage:
        async def process_one(self, retry_env):
            assert retry_env.external_id == "rss:1"
            return type("Result", (), {"salience": 0.9})()

    count = await run_due_retries_once(
        conn,
        triage=FakeTriage(),
        now_ts="2026-06-12T10:00:02+00:00",
        limit=10,
        max_attempts=3,
    )
    assert count == 1
    done = store.fetch_deferred_salience_retries(conn, state="done")
    assert done[0]["retry_id"] == retry_id


@pytest.mark.unit
async def test_retry_runner_reschedules_with_exponential_backoff(tmp_path):
    from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:1",
        text="Company reports earnings shock",
        source_tags={},
        raw_path="",
    )
    schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-deferred",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=1,
    )

    class FakeTriage:
        async def process_one(self, retry_env):
            return type("Result", (), {"salience": None})()

    count = await run_due_retries_once(
        conn,
        triage=FakeTriage(),
        now_ts="2026-06-12T10:00:02+00:00",
        limit=10,
        max_attempts=3,
    )
    assert count == 1
    pending = store.fetch_deferred_salience_retries(conn, state="pending")
    assert pending[0]["attempt_count"] == 1
    assert pending[0]["next_attempt_ts"] == "2026-06-12T10:02:02+00:00"
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/sensing/test_deferred_salience_retry.py -v
```

Expected: FAIL because `deferred_retry.py` does not exist.

- [ ] **Step 3: Implement deferred retry module**

Create `tradingagents/sensing/deferred_retry.py`:

```python
"""Durable retry workflow for deferred salience scoring."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.persistence import store
from tradingagents.sensing.envelope import Envelope


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _payload(env: Envelope) -> dict[str, Any]:
    return {
        "source": env.source,
        "ingested_ts": env.ingested_ts,
        "external_id": env.external_id,
        "text": env.text,
        "source_tags": env.source_tags,
        "raw_path": env.raw_path,
    }


def _hash_payload(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def envelope_from_payload(payload_json: str) -> Envelope:
    payload = json.loads(payload_json)
    return Envelope(
        source=payload["source"],
        ingested_ts=payload["ingested_ts"],
        external_id=payload.get("external_id"),
        text=payload["text"],
        source_tags=payload.get("source_tags") or {},
        raw_path=payload.get("raw_path") or "",
    )


def schedule_deferred_retry(
    conn: sqlite3.Connection,
    *,
    env: Envelope,
    event_id: str | None,
    reason: str,
    now_ts: str,
    base_delay_seconds: int,
) -> int:
    payload_json = json.dumps(_payload(env), sort_keys=True)
    next_attempt = _iso(_parse(now_ts) + timedelta(seconds=base_delay_seconds))
    return store.insert_deferred_salience_retry(
        conn,
        event_id=event_id,
        source=env.source,
        raw_path=env.raw_path,
        payload_hash=_hash_payload(payload_json),
        payload_json=payload_json,
        reason=reason,
        next_attempt_ts=next_attempt,
    )


def _next_attempt(now_ts: str, attempt_count: int, *, base_delay_seconds: int = 60, max_delay_seconds: int = 3600) -> str:
    delay = min(base_delay_seconds * (2 ** max(attempt_count, 0)), max_delay_seconds)
    return _iso(_parse(now_ts) + timedelta(seconds=delay))


async def run_due_retries_once(
    conn: sqlite3.Connection,
    *,
    triage,
    now_ts: str,
    limit: int,
    max_attempts: int,
) -> int:
    rows = store.claim_due_deferred_salience_retries(conn, now_ts=now_ts, limit=limit)
    handled = 0
    for row in rows:
        retry_id = int(row["retry_id"])
        try:
            result = await triage.process_one(envelope_from_payload(row["payload_json"]))
            handled += 1
            if getattr(result, "salience", None) is not None:
                store.mark_deferred_salience_retry_done(conn, retry_id=retry_id)
                continue
            if int(row["attempt_count"]) + 1 >= max_attempts:
                store.mark_deferred_salience_retry_dead(
                    conn,
                    retry_id=retry_id,
                    reason="max_attempts_exhausted",
                )
            else:
                store.reschedule_deferred_salience_retry(
                    conn,
                    retry_id=retry_id,
                    reason="still_deferred",
                    next_attempt_ts=_next_attempt(now_ts, int(row["attempt_count"]) + 1),
                )
        except Exception as exc:  # noqa: BLE001
            handled += 1
            if int(row["attempt_count"]) + 1 >= max_attempts:
                store.mark_deferred_salience_retry_dead(
                    conn,
                    retry_id=retry_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            else:
                store.reschedule_deferred_salience_retry(
                    conn,
                    retry_id=retry_id,
                    reason=f"{type(exc).__name__}: {exc}",
                    next_attempt_ts=_next_attempt(now_ts, int(row["attempt_count"]) + 1),
                )
    return handled
```

- [ ] **Step 4: Wire triage scheduling and retry loop**

In `tradingagents/sensing/triage.py`, import:

```python
from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry
```

In `Triage.process_one`, inside `if score.source == "deferred":`, after `insert_event(...)`, add:

```python
schedule_deferred_retry(
    self._conn,
    env=env,
    event_id=ev_id,
    reason=score.reason,
    now_ts=datetime.now(timezone.utc).isoformat(),
    base_delay_seconds=60,
)
```

In `_consume_forever`, before reading Redis entries in each loop iteration, add:

```python
await run_due_retries_once(
    self._conn,
    triage=self,
    now_ts=datetime.now(timezone.utc).isoformat(),
    limit=25,
    max_attempts=5,
)
```

- [ ] **Step 5: Run green**

Run:

```bash
python -m pytest tests/sensing/test_deferred_salience_retry.py -v
python -m pytest tests/sensing/test_triage_local_availability.py tests/sensing/test_triage_loop_nonblocking.py -q
```

Expected: PASS. Existing deferred tests should still pass, now with durable retry rows.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/sensing/deferred_retry.py tradingagents/sensing/triage.py tests/sensing/test_deferred_salience_retry.py
git commit -m "feat(triage): add durable deferred salience retry workflow"
```

---

## Phase 5 - Source Health Ledger

### Task 7: Source health helper and adapter wiring

**Files:**
- Create: `tradingagents/sensing/source_health.py`
- Create: `tests/sensing/test_source_health.py`
- Modify: `tradingagents/sensing/adapters/base.py`
- Modify: `tradingagents/sensing/adapters/gdelt.py`
- Modify: `tradingagents/sensing/adapters/telegram.py`
- Modify: `tradingagents/sensing/adapters/polygon_news.py`
- Modify: `tradingagents/sensing/adapters/rss.py`
- Modify: `tradingagents/sensing/adapters/macro.py`
- Modify: `tradingagents/sensing/adapters/x.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/sensing/test_source_health.py`:

```python
import json
from unittest.mock import MagicMock, patch

import fakeredis.aioredis
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
async def test_gdelt_success_updates_source_health(conn, tmp_path):
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter

    payload = {
        "articles": [{
            "url": "https://news.example/g-1",
            "title": "Macro shock",
            "seendate": "20260612T140000Z",
            "domain": "news.example",
        }],
    }
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status = lambda: None
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        adapter = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"), stream="ingest:raw")
        emitted = await adapter.poll_once(redis=redis, conn=conn)
    assert emitted == 1
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["service_name"] == "adapter-gdelt"
    assert row["last_success_ts"] is not None
    assert row["last_event_ts"] is not None
    assert row["cursor"] == "20260612T140000Z"
    assert row["events_emitted_last_poll"] == 1
    assert row["consecutive_failures"] == 0


@pytest.mark.unit
async def test_gdelt_failure_updates_source_health(conn, tmp_path):
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter

    with patch("tradingagents.sensing.adapters.gdelt.requests.get", side_effect=RuntimeError("boom")):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        adapter = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"), stream="ingest:raw")
        emitted = await adapter.poll_once(redis=redis, conn=conn)
    assert emitted == 0
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["consecutive_failures"] == 1
    assert "boom" in row["last_error"]


@pytest.mark.unit
async def test_telegram_message_records_channel_diagnostics(conn, tmp_path):
    from tradingagents.sensing.adapters.telegram import _on_message

    class Msg:
        message = "NVDA earnings leak"
        id = 7
        date = type("Date", (), {"isoformat": lambda self: "2026-06-12T10:00:00+00:00"})()

    class Chat:
        username = "earningswire"

    event = type("Event", (), {"message": Msg(), "chat": Chat()})()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _on_message(event, redis=redis, conn=conn, stream="ingest:raw", staging_root=str(tmp_path / "s"))
    row = store.fetch_source_health(conn)["telegram"]
    diagnostics = json.loads(row["diagnostics"])
    assert diagnostics["resolved_channels"] == ["earningswire"]
    assert row["events_emitted_last_poll"] == 1
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/sensing/test_source_health.py -v
```

Expected: FAIL because adapter health wiring does not exist.

- [ ] **Step 3: Implement source health module**

Create `tradingagents/sensing/source_health.py`:

```python
"""Source health ledger helpers for sensing adapters."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from tradingagents.persistence import store


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_poll_success(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    emitted: int,
    cursor: Optional[str],
    last_event_ts: Optional[str],
    diagnostics: Optional[dict] = None,
) -> None:
    now = now_iso()
    store.upsert_source_health_success(
        conn,
        source=source,
        service_name=service_name,
        last_poll_ts=now,
        last_success_ts=now,
        last_event_ts=last_event_ts,
        cursor=cursor,
        cursor_updated_ts=now if cursor is not None else None,
        events_emitted_last_poll=emitted,
        diagnostics=diagnostics or {},
    )


def record_poll_failure(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    error: BaseException | str,
    diagnostics: Optional[dict] = None,
) -> None:
    store.upsert_source_health_failure(
        conn,
        source=source,
        service_name=service_name,
        last_poll_ts=now_iso(),
        error=str(error),
        diagnostics=diagnostics or {},
    )
```

- [ ] **Step 4: Wire GDELT and Telegram exactly**

In `tradingagents/sensing/adapters/gdelt.py`, import `record_poll_failure` and `record_poll_success`. Replace the `except` block in `poll_once` with:

```python
        except Exception as e:
            log.warning("gdelt poll failed: %s", e)
            record_poll_failure(
                conn,
                source=NAME,
                service_name="adapter-gdelt",
                error=e,
                diagnostics={"query": self._query},
            )
            return 0
```

After the article loop, before `return emitted`, add:

```python
        record_poll_success(
            conn,
            source=NAME,
            service_name="adapter-gdelt",
            emitted=emitted,
            cursor=new_cursor or None,
            last_event_ts=(
                datetime.now(timezone.utc).isoformat() if emitted else None
            ),
            diagnostics={"query": self._query},
        )
```

In `tradingagents/sensing/adapters/telegram.py`, import `record_poll_success`. At the end of `_on_message`, add:

```python
    record_poll_success(
        conn,
        source=NAME,
        service_name="adapter-telegram",
        emitted=1,
        cursor=json.dumps(cursors),
        last_event_ts=datetime.now(timezone.utc).isoformat(),
        diagnostics={"resolved_channels": sorted(cursors.keys())},
    )
```

- [ ] **Step 5: Wire the remaining adapters with explicit health calls**

For `polygon_news.py`, `rss.py`, `macro.py`, and `x.py`, add this import at the top of each file:

```python
from tradingagents.sensing.source_health import record_poll_failure, record_poll_success
```

On every caught poll exception:

```python
record_poll_failure(
    conn,
    source=NAME,
    service_name=f"adapter-{NAME}",
    error=e,
    diagnostics={},
)
```

On every successful poll or batch:

```python
record_poll_success(
    conn,
    source=NAME,
    service_name=f"adapter-{NAME}",
    emitted=emitted,
    cursor=new_cursor or None,
    last_event_ts=datetime.now(timezone.utc).isoformat() if emitted else None,
    diagnostics={},
)
```

Apply these concrete arguments in each adapter:

```python
# polygon_news.py
source=NAME
service_name="adapter-polygon"
cursor=new_cursor or last_seen or None
diagnostics={"vendor": "polygon_news"}

# rss.py
source=NAME
service_name="adapter-rss"
cursor=new_cursor or None
diagnostics={"feeds": self._feeds}

# macro.py
source=NAME
service_name="adapter-macro"
cursor=cursor or None
diagnostics={"series": list(self._series)}

# x.py
source=NAME
service_name="adapter-x"
cursor=newest_id or None
diagnostics={"query": self._query}
```

If one of these attributes has a different local name in the file at execution time, bind it to the name shown above immediately before the `record_poll_success(...)` call so the inserted call remains exactly the same.

- [ ] **Step 6: Run green**

Run:

```bash
python -m pytest tests/sensing/test_source_health.py tests/sensing/test_adapter_gdelt.py tests/sensing/test_adapter_telegram.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/sensing/source_health.py tradingagents/sensing/adapters/base.py tradingagents/sensing/adapters/gdelt.py tradingagents/sensing/adapters/telegram.py tradingagents/sensing/adapters/polygon_news.py tradingagents/sensing/adapters/rss.py tradingagents/sensing/adapters/macro.py tradingagents/sensing/adapters/x.py tests/sensing/test_source_health.py
git commit -m "feat(sensing): record adapter source health"
```

---

## Phase 6 - Ordered Delivery Attempt Chains

### Task 8: Telegram-primary/email-fallback delivery policy

**Files:**
- Create: `tradingagents/delivery/policy.py`
- Create: `tests/delivery/test_ordered_policy.py`
- Modify: `tradingagents/secretary/service.py`
- Modify: `tradingagents/default_config.py`

- [ ] **Step 1: Write the failing policy tests**

Create `tests/delivery/test_ordered_policy.py`:

```python
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


class FakeChannel:
    def __init__(self, *, conn, name, status):
        self._conn = conn
        self.channel_name = name
        self.status = status

    def send(self, *, brief, mode, body, delivery_group_id=None, attempt_rank=None, fallback_of=None, is_fallback=False):
        return store.insert_delivery(
            self._conn,
            brief_id=brief["brief_id"],
            channel=self.channel_name,
            status=self.status,
            sent_ts="2026-06-12T10:00:00+00:00" if self.status == "sent" else None,
            channel_ref=f"{self.channel_name}:1" if self.status == "sent" else None,
            skip_reason="quiet_hours" if self.status == "skipped" else None,
            delivery_group_id=delivery_group_id,
            attempt_rank=attempt_rank,
            fallback_of=fallback_of,
            is_fallback=is_fallback,
            failure_reason="failed" if self.status == "failed" else None,
        )


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        c,
        brief_id="b1",
        mode="event_alert_light",
        scope='["NVDA"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    return c


@pytest.mark.unit
def test_telegram_success_suppresses_email(conn):
    from tradingagents.delivery.policy import deliver_ordered

    result = deliver_ordered(
        conn=conn,
        brief={"brief_id": "b1", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg", "email": "em"},
        channels={
            "telegram": FakeChannel(conn=conn, name="telegram", status="sent"),
            "email": FakeChannel(conn=conn, name="email", status="sent"),
        },
        urgent=False,
    )
    assert result.final_status == "sent"
    groups = store.fetch_delivery_groups(conn)
    attempts = next(iter(groups.values()))
    assert [a["channel"] for a in attempts] == ["telegram"]


@pytest.mark.unit
def test_telegram_failure_triggers_email_fallback(conn):
    from tradingagents.delivery.policy import deliver_ordered

    result = deliver_ordered(
        conn=conn,
        brief={"brief_id": "b1", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg", "email": "em"},
        channels={
            "telegram": FakeChannel(conn=conn, name="telegram", status="failed"),
            "email": FakeChannel(conn=conn, name="email", status="sent"),
        },
        urgent=False,
    )
    assert result.final_status == "sent"
    attempts = next(iter(store.fetch_delivery_groups(conn).values()))
    assert [(a["channel"], a["status"], a["attempt_rank"]) for a in attempts] == [
        ("telegram", "failed", 1),
        ("email", "sent", 2),
    ]
    assert attempts[1]["fallback_of"] == attempts[0]["delivery_id"]
    assert attempts[1]["is_fallback"] == 1


@pytest.mark.unit
def test_quiet_hours_skip_does_not_email_unless_urgent(conn):
    from tradingagents.delivery.policy import deliver_ordered

    deliver_ordered(
        conn=conn,
        brief={"brief_id": "b1", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg", "email": "em"},
        channels={
            "telegram": FakeChannel(conn=conn, name="telegram", status="skipped"),
            "email": FakeChannel(conn=conn, name="email", status="sent"),
        },
        urgent=False,
    )
    attempts = next(iter(store.fetch_delivery_groups(conn).values()))
    assert [a["channel"] for a in attempts] == ["telegram"]
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/delivery/test_ordered_policy.py -v
```

Expected: FAIL because `delivery.policy` does not exist and channels do not accept chain kwargs.

- [ ] **Step 3: Update `DeliveryChannel.send` signature**

In `tradingagents/delivery/base.py`, change:

```python
def send(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
```

to:

```python
def send(
    self,
    *,
    brief: Dict[str, Any],
    mode: str,
    body: str,
    delivery_group_id: Optional[str] = None,
    attempt_rank: Optional[int] = None,
    fallback_of: Optional[int] = None,
    is_fallback: bool = False,
) -> int:
```

In all three `store.insert_delivery(...)` calls inside `send`, pass:

```python
delivery_group_id=delivery_group_id,
attempt_rank=attempt_rank,
fallback_of=fallback_of,
is_fallback=is_fallback,
failure_reason=("quiet_hours" if status == "skipped" else None),
```

For the exception path, set:

```python
failure_reason=str(exc)[:1000],
```

Update `TelegramOutbound.send` and `EmailOutbound.send` to accept the same chain kwargs and forward them to `super().send(...)`. For skipped disabled channels, pass the chain fields into `store.insert_delivery`.

- [ ] **Step 4: Implement ordered policy**

Create `tradingagents/delivery/policy.py`:

```python
"""Ordered delivery policy: Telegram primary, email fallback."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class DeliveryPolicyResult:
    delivery_group_id: str
    final_status: str
    attempt_ids: list[int]


def deliver_ordered(
    *,
    conn: sqlite3.Connection,
    brief: dict[str, Any],
    mode: str,
    bodies: dict[str, str],
    channels: dict[str, Any],
    urgent: bool = False,
) -> DeliveryPolicyResult:
    group_id = uuid.uuid4().hex
    attempt_ids: list[int] = []

    telegram = channels.get("telegram")
    email = channels.get("email")

    if telegram is None and email is None:
        return DeliveryPolicyResult(group_id, "skipped", [])

    primary_id = None
    primary_status = "skipped"
    if telegram is not None:
        primary_id = telegram.send(
            brief=brief,
            mode=mode,
            body=bodies.get("telegram", ""),
            delivery_group_id=group_id,
            attempt_rank=1,
            fallback_of=None,
            is_fallback=False,
        )
        attempt_ids.append(primary_id)
        row = conn.execute(
            "SELECT status, skip_reason FROM deliveries WHERE delivery_id = ?",
            (primary_id,),
        ).fetchone()
        primary_status = row["status"]
        if primary_status == "sent":
            return DeliveryPolicyResult(group_id, "sent", attempt_ids)
        if primary_status == "skipped" and row["skip_reason"] == "quiet_hours" and not urgent:
            return DeliveryPolicyResult(group_id, "skipped", attempt_ids)

    if email is None:
        return DeliveryPolicyResult(group_id, primary_status, attempt_ids)

    fallback_id = email.send(
        brief=brief,
        mode=mode,
        body=bodies.get("email", ""),
        delivery_group_id=group_id,
        attempt_rank=2,
        fallback_of=primary_id,
        is_fallback=True,
    )
    attempt_ids.append(fallback_id)
    row = conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id = ?",
        (fallback_id,),
    ).fetchone()
    return DeliveryPolicyResult(group_id, row["status"], attempt_ids)
```

- [ ] **Step 5: Replace Secretary fan-out loops**

In `tradingagents/secretary/service.py`, create a local helper:

```python
def _deliver_with_policy(conn, config, *, brief, mode, urgent=False) -> None:
    from tradingagents.delivery.policy import deliver_ordered
    from tradingagents.delivery.render import render_for_channel

    channels = {}
    for name in ("telegram", "email"):
        ch = _build_channel(name, conn, config)
        if ch is not None:
            channels[name] = ch
    bodies = {
        name: render_for_channel(channel=name, mode=mode, brief=brief)
        for name in channels
    }
    deliver_ordered(
        conn=conn,
        brief=brief,
        mode=mode,
        bodies=bodies,
        channels=channels,
        urgent=urgent,
    )
```

In `_deliver_light_alert`, `_deliver_deep_dive`, and `_deliver_event_alert`, replace the loop over `enabled_channels` with:

```python
_deliver_with_policy(self._conn, config, brief=brief, mode="event_alert_light")
```

Use the matching mode string in each method.

In `tradingagents/default_config.py`, add:

```python
"delivery_policy": "ordered_telegram_email",
```

and a nested env override:

```python
policy = os.environ.get("IIC_DELIVERY_POLICY")
if policy:
    config["delivery_policy"] = policy
```

- [ ] **Step 6: Run green**

Run:

```bash
python -m pytest tests/delivery/test_ordered_policy.py tests/delivery -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/delivery/base.py tradingagents/delivery/telegram.py tradingagents/delivery/email.py tradingagents/delivery/policy.py tradingagents/secretary/service.py tradingagents/default_config.py tests/delivery/test_ordered_policy.py
git commit -m "feat(delivery): add ordered telegram email fallback chains"
```

---

## Phase 7 - Worker Lanes and Capacity Evidence

### Task 9: Lane-aware queue leasing and worker config

**Files:**
- Modify: `tradingagents/orchestrator/queue_store.py`
- Modify: `tradingagents/orchestrator/worker.py`
- Modify: `tradingagents/default_config.py`
- Create: `tests/orchestrator/test_worker_lanes.py`

- [ ] **Step 1: Write the failing lane tests**

Create `tests/orchestrator/test_worker_lanes.py`:

```python
import json

import pytest

from tradingagents.persistence.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
def test_insert_queue_job_sets_lane(conn):
    from tradingagents.orchestrator import queue_store

    job_id = queue_store.insert_queue_job(
        conn,
        job_type="run_full_study",
        payload=json.dumps({"ticker": "NVDA"}),
        trigger_event_id=None,
        lane="deep",
        timeout_seconds=1200,
    )
    row = conn.execute("SELECT lane, timeout_seconds FROM queue_jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["lane"] == "deep"
    assert row["timeout_seconds"] == 1200


@pytest.mark.unit
def test_lease_one_only_claims_matching_lane(conn):
    from tradingagents.orchestrator import queue_store

    queue_store.insert_queue_job(conn, job_type="refine_brief", payload="{}", trigger_event_id=None, lane="action")
    queue_store.insert_queue_job(conn, job_type="run_full_study", payload="{}", trigger_event_id=None, lane="deep")

    deep = queue_store.lease_one(conn, lane="deep")
    assert deep["job_type"] == "run_full_study"
    action = queue_store.lease_one(conn, lane="action")
    assert action["job_type"] == "refine_brief"


@pytest.mark.unit
def test_queue_lane_depth(conn):
    from tradingagents.orchestrator import queue_store

    queue_store.insert_queue_job(conn, job_type="refine_brief", payload="{}", trigger_event_id=None, lane="action")
    queue_store.insert_queue_job(conn, job_type="run_full_study", payload="{}", trigger_event_id=None, lane="deep")
    assert queue_store.lane_depth(conn) == {
        "action": {"queued": 1},
        "deep": {"queued": 1},
    }
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/orchestrator/test_worker_lanes.py -v
```

Expected: FAIL because queue helpers are not lane-aware.

- [ ] **Step 3: Update queue store**

In `tradingagents/orchestrator/queue_store.py`, change `insert_queue_job` signature:

```python
def insert_queue_job(
    conn: sqlite3.Connection,
    *,
    job_type: str,
    payload: str,
    trigger_event_id: Optional[str],
    lane: str = "deep",
    timeout_seconds: Optional[int] = None,
) -> int:
```

Use:

```python
cur = conn.execute(
    "INSERT INTO queue_jobs (job_type, payload, state, enqueued_ts, "
    "trigger_event_id, lane, timeout_seconds) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
    (job_type, payload, _now_iso(), trigger_event_id, lane, timeout_seconds),
)
```

Change `lease_one` signature:

```python
def lease_one(conn: sqlite3.Connection, *, lane: Optional[str] = None) -> Optional[sqlite3.Row]:
```

Use:

```python
lane_filter = "AND lane = ?" if lane is not None else ""
params = [_now_iso()]
if lane is not None:
    params.append(lane)
row = conn.execute(
    f"""
    UPDATE queue_jobs
       SET state = 'running',
           started_ts = ?,
           heartbeat_ts = ?
     WHERE job_id = (
         SELECT job_id FROM queue_jobs
          WHERE state = 'queued'
          {lane_filter}
          ORDER BY job_id
          LIMIT 1
     )
 RETURNING job_id, job_type, payload, trigger_event_id, state, started_ts, lane, timeout_seconds
    """,
    (params[0], *params),
).fetchone()
```

Add:

```python
def lane_depth(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        "SELECT lane, state, COUNT(*) AS n FROM queue_jobs GROUP BY lane, state"
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row["lane"], {})[row["state"]] = int(row["n"])
    return out
```

- [ ] **Step 4: Update worker config**

In `tradingagents/default_config.py`, add env override handling:

```python
worker_lane = os.environ.get("IIC_WORKER_LANE")
if worker_lane:
    config["worker_lane"] = worker_lane
worker_concurrency = os.environ.get("IIC_WORKER_CONCURRENCY")
if worker_concurrency:
    config["max_concurrent_jobs"] = int(worker_concurrency)
worker_timeout = os.environ.get("IIC_WORKER_JOB_TIMEOUT_MIN")
if worker_timeout:
    config["worker_job_timeout_min"] = int(worker_timeout)
```

Add defaults:

```python
"worker_lane": "deep",
"worker_lane_timeouts": {"action": 300, "deep": 1200},
```

In `tradingagents/orchestrator/worker.py`, call:

```python
job = queue_store.lease_one(conn, lane=cfg.get("worker_lane"))
```

and log the lane in the startup line.

- [ ] **Step 5: Run green**

Run:

```bash
python -m pytest tests/orchestrator/test_worker_lanes.py tests/orchestrator/test_worker_loop.py tests/orchestrator/test_queue_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/orchestrator/queue_store.py tradingagents/orchestrator/worker.py tradingagents/default_config.py tests/orchestrator/test_worker_lanes.py
git commit -m "feat(orchestrator): add lane-aware worker leasing"
```

---

## Phase 8 - Dashboard and Focused Soak Gate

### Task 10: Shared operational query layer and dashboard tab

**Files:**
- Create: `tradingagents/dashboard/panels/operations.py`
- Create: `tests/dashboard/test_operations_panel.py`
- Modify: `tradingagents/dashboard/app.py`

- [ ] **Step 1: Write the failing dashboard query tests**

Create `tests/dashboard/test_operations_panel.py`:

```python
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_operations_snapshot_reads_shared_evidence(tmp_path):
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    conn = connect(str(tmp_path / "iic.db"))
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:00:00+00:00",
        last_success_ts="2026-06-12T10:00:00+00:00",
        last_event_ts="2026-06-12T10:00:00+00:00",
        cursor="c1",
        cursor_updated_ts="2026-06-12T10:00:00+00:00",
        events_emitted_last_poll=1,
        diagnostics={},
    )
    store.insert_llm_call(
        conn,
        created_ts="2026-06-12T10:00:00+00:00",
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen",
        base_url="http://local",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        status="success",
        latency_ms=50,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=0.0,
        error_class=None,
        error_message=None,
    )
    snap = fetch_operations_snapshot(conn, now_ts="2026-06-12T10:05:00+00:00")
    assert snap["sources"]["gdelt"]["consecutive_failures"] == 0
    assert snap["llm_calls"]["triage_salience"]["total"] == 1
    assert snap["llm_calls"]["triage_salience"]["parse_failures"] == 0
    assert "deferred_salience" in snap
    assert "delivery_groups" in snap
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/dashboard/test_operations_panel.py -v
```

Expected: FAIL because operations panel does not exist.

- [ ] **Step 3: Implement operations panel query helper**

Create `tradingagents/dashboard/panels/operations.py`:

```python
"""Operational status queries shared by dashboard and focused gates."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from tradingagents.persistence import store
from tradingagents.orchestrator import queue_store


def _dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _age_seconds(now_ts: str, ts: str | None) -> float | None:
    now = _dt(now_ts)
    other = _dt(ts)
    if now is None or other is None:
        return None
    return (now - other).total_seconds()


def fetch_llm_role_summary(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT role, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success, "
        "SUM(CASE WHEN status = 'parse_error' THEN 1 ELSE 0 END) AS parse_failures, "
        "SUM(CASE WHEN status IN ('transport_error', 'timeout') THEN 1 ELSE 0 END) AS transport_failures, "
        "SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_used, "
        "AVG(latency_ms) AS avg_latency_ms "
        "FROM llm_calls GROUP BY role"
    ).fetchall()
    return {r["role"]: dict(r) for r in rows}


def fetch_deferred_summary(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM deferred_salience_retry GROUP BY state"
    ).fetchall()
    return {r["state"]: int(r["n"]) for r in rows}


def fetch_failed_delivery_groups(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT delivery_group_id, COUNT(*) AS attempts, "
        "SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent "
        "FROM deliveries WHERE delivery_group_id IS NOT NULL "
        "GROUP BY delivery_group_id HAVING sent = 0"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_operations_snapshot(conn: sqlite3.Connection, *, now_ts: str) -> dict[str, Any]:
    sources = store.fetch_source_health(conn)
    for item in sources.values():
        item["last_poll_age_seconds"] = _age_seconds(now_ts, item.get("last_poll_ts"))
        item["last_event_age_seconds"] = _age_seconds(now_ts, item.get("last_event_ts"))
    return {
        "sources": sources,
        "llm_calls": fetch_llm_role_summary(conn),
        "deferred_salience": fetch_deferred_summary(conn),
        "queue_lanes": queue_store.lane_depth(conn),
        "delivery_groups": {
            "failed": fetch_failed_delivery_groups(conn),
        },
        "costs": __import__("tradingagents.dashboard.panels.costs", fromlist=["fetch_provider_split"]).fetch_provider_split(conn),
    }
```

- [ ] **Step 4: Add dashboard Operations tab**

In `tradingagents/dashboard/app.py`, change:

```python
tab_briefs, tab_costs, tab_queue, tab_actions = st.tabs(
    ["Briefs", "Costs", "Queue", "Actions"]
)
```

to:

```python
tab_ops, tab_briefs, tab_costs, tab_queue, tab_actions = st.tabs(
    ["Operations", "Briefs", "Costs", "Queue", "Actions"]
)
```

Before the Briefs tab block, add:

```python
with tab_ops:
    st.header("Operational status")
    from datetime import datetime, timezone
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    snap = fetch_operations_snapshot(_conn(), now_ts=datetime.now(timezone.utc).isoformat())
    source_rows = list(snap["sources"].values())
    st.subheader("Sources")
    st.dataframe(source_rows or [{"info": "no source health rows yet"}], use_container_width=True)
    st.subheader("LLM calls")
    llm_rows = [{"role": role, **values} for role, values in snap["llm_calls"].items()]
    st.dataframe(llm_rows or [{"info": "no llm call rows yet"}], use_container_width=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Deferred pending", snap["deferred_salience"].get("pending", 0))
    c2.metric("Deferred dead", snap["deferred_salience"].get("dead", 0))
    c3.metric("Failed delivery groups", len(snap["delivery_groups"]["failed"]))
    st.subheader("Queue lanes")
    lane_rows = [
        {"lane": lane, **states}
        for lane, states in snap["queue_lanes"].items()
    ]
    st.dataframe(lane_rows or [{"info": "no queue jobs yet"}], use_container_width=True)
```

- [ ] **Step 5: Run green**

Run:

```bash
python -m pytest tests/dashboard/test_operations_panel.py tests/dashboard -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/dashboard/panels/operations.py tradingagents/dashboard/app.py tests/dashboard/test_operations_panel.py
git commit -m "feat(dashboard): add operations status panel"
```

### Task 11: Focused soak gate over the same evidence

**Files:**
- Create: `scripts/focused_soak_gate.py`
- Create: `tests/scripts/test_focused_soak_gate.py`
- Modify: `scripts/f4_f5_exit_gate.py`

- [ ] **Step 1: Write the failing gate tests**

Create `tests/scripts/test_focused_soak_gate.py`:

```python
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_focused_gate_passes_with_healthy_seed(tmp_path):
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:00:00+00:00",
        last_success_ts="2026-06-12T10:00:00+00:00",
        last_event_ts="2026-06-12T10:00:00+00:00",
        cursor="c1",
        cursor_updated_ts="2026-06-12T10:00:00+00:00",
        events_emitted_last_poll=1,
        diagnostics={},
    )
    store.insert_llm_call(
        conn,
        created_ts="2026-06-12T10:00:00+00:00",
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen",
        base_url="http://local",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        status="success",
        latency_ms=40,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=0.0,
        error_class=None,
        error_message=None,
    )
    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        old_service_checker=lambda: [],
        redis_checker=lambda: {"ok": True, "appendonly": "yes"},
    )
    assert report["pass"] is True
    assert report["checks"]["sources_fresh"]["pass"] is True
    assert report["checks"]["llm_calls_present"]["pass"] is True


@pytest.mark.unit
def test_focused_gate_fails_stale_source(tmp_path):
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T09:00:00+00:00",
        last_success_ts="2026-06-12T09:00:00+00:00",
        last_event_ts="2026-06-12T09:00:00+00:00",
        cursor="c1",
        cursor_updated_ts="2026-06-12T09:00:00+00:00",
        events_emitted_last_poll=1,
        diagnostics={},
    )
    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        old_service_checker=lambda: [],
        redis_checker=lambda: {"ok": True, "appendonly": "yes"},
    )
    assert report["pass"] is False
    assert report["checks"]["sources_fresh"]["pass"] is False
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/scripts/test_focused_soak_gate.py -v
```

Expected: FAIL because `scripts/focused_soak_gate.py` does not exist.

- [ ] **Step 3: Implement focused gate**

Create `scripts/focused_soak_gate.py`:

```python
#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tradingagents.dashboard.panels.operations import fetch_operations_snapshot
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.db import connect


def _age_ok(value: float | None, threshold: int) -> bool:
    return value is not None and value <= threshold


def default_old_service_checker() -> list[str]:
    names = [
        "iic-triage.service",
        "iic-promoter.service",
        "iic-worker.service",
        "redis-server.service",
    ]
    active: list[str] = []
    for name in names:
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", name],
                stderr=subprocess.STDOUT,
                timeout=5,
            ).decode().strip()
        except Exception:
            out = "inactive"
        if out == "active":
            active.append(name)
    return active


def default_redis_checker() -> dict[str, Any]:
    try:
        ping = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "ping"],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode()
        appendonly = subprocess.check_output(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "CONFIG", "GET", "appendonly"],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": "PONG" in ping, "appendonly": "yes" if "yes" in appendonly else "no"}


def evaluate(
    conn,
    *,
    now_ts: str,
    enabled_sources: list[str],
    source_stale_after_seconds: int,
    deferred_pending_max: int,
    failed_delivery_group_max: int,
    allow_api_classification_spend: bool,
    old_service_checker: Callable[[], list[str]],
    redis_checker: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    snap = fetch_operations_snapshot(conn, now_ts=now_ts)
    checks: dict[str, dict[str, Any]] = {}

    old_active = old_service_checker()
    checks["old_services_stopped"] = {
        "pass": old_active == [],
        "detail": f"active old services: {old_active or 'none'}",
    }

    redis = redis_checker()
    checks["redis_owned_and_configured"] = {
        "pass": bool(redis.get("ok")) and redis.get("appendonly") == "yes",
        "detail": json.dumps(redis, sort_keys=True),
    }

    stale = []
    for source in enabled_sources:
        row = snap["sources"].get(source)
        if row is None:
            stale.append(f"{source}:missing")
            continue
        if row["consecutive_failures"] > 0:
            stale.append(f"{source}:failures={row['consecutive_failures']}")
        if not _age_ok(row.get("last_poll_age_seconds"), source_stale_after_seconds):
            stale.append(f"{source}:last_poll_age={row.get('last_poll_age_seconds')}")
    checks["sources_fresh"] = {
        "pass": stale == [],
        "detail": f"stale sources: {stale or 'none'}",
    }

    deferred = snap["deferred_salience"]
    pending = int(deferred.get("pending", 0))
    checks["deferred_retry_bounded"] = {
        "pass": pending <= deferred_pending_max,
        "detail": f"pending={pending} max={deferred_pending_max} states={deferred}",
    }

    llm = snap["llm_calls"]
    classification_calls = (
        llm.get("triage_salience", {}).get("total", 0)
        + llm.get("alert_gate", {}).get("total", 0)
    )
    parse_failures = sum(int(v.get("parse_failures") or 0) for v in llm.values())
    transport_failures = sum(int(v.get("transport_failures") or 0) for v in llm.values())
    checks["llm_calls_present"] = {
        "pass": classification_calls > 0,
        "detail": f"classification_calls={classification_calls}",
    }
    checks["llm_failures_bounded"] = {
        "pass": parse_failures == 0 and transport_failures == 0,
        "detail": f"parse_failures={parse_failures} transport_failures={transport_failures}",
    }

    api_spend = float(snap["costs"].get("api_spend", 0.0))
    checks["no_unexpected_api_classification_spend"] = {
        "pass": allow_api_classification_spend or api_spend == 0.0,
        "detail": f"api_spend={api_spend:.6f}",
    }

    failed_groups = snap["delivery_groups"]["failed"]
    checks["delivery_groups_bounded"] = {
        "pass": len(failed_groups) <= failed_delivery_group_max,
        "detail": f"failed_groups={len(failed_groups)} max={failed_delivery_group_max}",
    }

    return {
        "generated_ts": now_ts,
        "checks": checks,
        "pass": all(check["pass"] for check in checks.values()),
        "snapshot": snap,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "soak"), default="soak")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = connect(DEFAULT_CONFIG["iic_db_path"])
    report = evaluate(
        conn,
        now_ts=datetime.now(timezone.utc).isoformat(),
        enabled_sources=[k for k, v in DEFAULT_CONFIG["sensing_adapters_enabled"].items() if v],
        source_stale_after_seconds=int(DEFAULT_CONFIG.get("source_stale_after_seconds", 1800)),
        deferred_pending_max=int(DEFAULT_CONFIG.get("deferred_retry_max_pending", 0)),
        failed_delivery_group_max=int(DEFAULT_CONFIG.get("delivery_failed_group_max", 0)),
        allow_api_classification_spend=DEFAULT_CONFIG.get("allow_api_classification_spend", False),
        old_service_checker=default_old_service_checker,
        redis_checker=default_redis_checker,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        mark = "PASS" if report["pass"] else "FAIL"
        print(f"# Focused Soak Gate - {mark}")
        for name, check in report["checks"].items():
            print(f"- {name}: {'PASS' if check['pass'] else 'FAIL'} - {check['detail']}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

In `tradingagents/default_config.py`, add defaults:

```python
"source_stale_after_seconds": 1800,
"deferred_retry_max_pending": 0,
"delivery_failed_group_max": 0,
"allow_api_classification_spend": False,
```

- [ ] **Step 4: Run green**

Run:

```bash
python -m pytest tests/scripts/test_focused_soak_gate.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/focused_soak_gate.py tradingagents/default_config.py tests/scripts/test_focused_soak_gate.py
git commit -m "feat(ops): add focused soak gate over shared evidence"
```

---

## Phase 9 - Script Entrypoints and Runbook

### Task 12: Direct script import bootstrap

**Files:**
- Create: `scripts/_repo_bootstrap.py`
- Create: `tests/scripts/test_repo_bootstrap.py`
- Modify: `scripts/focused_soak_gate.py`
- Modify: `scripts/f4_f5_exit_gate.py`
- Modify: `scripts/f5_exit_gate.py`
- Modify: `scripts/shadow_eval.py`

- [ ] **Step 1: Write the failing subprocess tests**

Create `tests/scripts/test_repo_bootstrap.py`:

```python
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_focused_soak_gate_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/focused_soak_gate.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "Focused Soak Gate" in result.stdout or "--mode" in result.stdout


@pytest.mark.unit
def test_shadow_eval_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/shadow_eval.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--help" in result.stdout or "usage:" in result.stdout
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/scripts/test_repo_bootstrap.py -v
```

Expected: FAIL for any script that imports `tradingagents` without the repo root on `sys.path`.

- [ ] **Step 3: Add bootstrap**

Create `scripts/_repo_bootstrap.py`:

```python
"""Make direct `python scripts/foo.py` execution import the repo package."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_root_on_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root
```

At the top of each modified script, before importing `tradingagents`, add:

```python
try:
    from scripts._repo_bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:
    from _repo_bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()
```

- [ ] **Step 4: Run green**

Run:

```bash
python -m pytest tests/scripts/test_repo_bootstrap.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/_repo_bootstrap.py scripts/focused_soak_gate.py scripts/f4_f5_exit_gate.py scripts/f5_exit_gate.py scripts/shadow_eval.py tests/scripts/test_repo_bootstrap.py
git commit -m "fix(scripts): support direct script execution from repo root"
```

### Task 13: Service platform launch runbook

**Files:**
- Create: `ops/runbooks/service-platform.md`
- Modify: `README.md`
- Create: `tests/ops/test_service_platform_runbook.py`

- [ ] **Step 1: Write the failing runbook test**

Create `tests/ops/test_service_platform_runbook.py`:

```python
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_service_platform_runbook_covers_launch_and_rollback():
    text = (ROOT / "ops" / "runbooks" / "service-platform.md").read_text()
    required = [
        "docker compose --profile runtime --profile sources --profile dashboard up -d",
        "python scripts/focused_soak_gate.py --mode preflight --json",
        "Old Service Shutdown",
        "Redis Ownership",
        "External Local LLM",
        "Deferred Salience Retry",
        "Delivery Fallback",
        "Rollback",
    ]
    for needle in required:
        assert needle in text
    assert "/home/ziwei-huang/TradingAgents/TradingAgents" not in text
    assert "iic-redis" not in text
```

- [ ] **Step 2: Run red**

Run:

```bash
python -m pytest tests/ops/test_service_platform_runbook.py -v
```

Expected: FAIL because runbook does not exist.

- [ ] **Step 3: Create runbook**

Create `ops/runbooks/service-platform.md`:

````markdown
# IIC-Forge Service Platform Runbook

## Launch

```bash
cd /opt/iic-forge
cp ops/env.iic-forge.example .env
$EDITOR .env
docker compose --profile runtime --profile sources --profile dashboard up -d
python scripts/focused_soak_gate.py --mode preflight --json
```

## Old Service Shutdown

Disable old per-daemon host services before the Compose runtime becomes authoritative:

```bash
sudo systemctl disable --now iic-triage.service iic-promoter.service iic-worker.service redis-server.service || true
systemctl is-active iic-triage.service iic-promoter.service iic-worker.service redis-server.service
```

The focused soak gate must report `old_services_stopped: PASS`.

## Redis Ownership

Redis is the `redis` service in `compose.yml`. Confirm the checked-in config is loaded:

```bash
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli CONFIG GET appendonly
docker compose exec redis redis-cli CONFIG GET maxmemory-policy
```

## External Local LLM

The local model server stays outside Compose. Configure only the URL/model/provider in `.env`:

```dotenv
LOCAL_LLM_BASE_URL=http://host.docker.internal:8080/v1
IIC_TRIAGE_LLM_PROVIDER=local
IIC_TRIAGE_LLM_MODEL=qwen3.6-27b-instruct-q4_k_m
IIC_ALERT_GATE_LLM_PROVIDER=local
IIC_ALERT_GATE_LLM_MODEL=qwen3.6-27b-instruct-q4_k_m
```

Restart only the dependent services after changing local LLM variables:

```bash
docker compose restart triage promoter
```

## Deferred Salience Retry

Deferred rows are durable in `deferred_salience_retry`. Healthy launch state has pending rows below the gate threshold and no dead rows:

```bash
sqlite3 /srv/iic-forge/data/iic.db \
  "select state, count(*) from deferred_salience_retry group by state;"
```

## Delivery Fallback

Delivery is ordered: Telegram is attempt rank 1, email fallback is rank 2. Telegram quiet-hours skip does not send email unless a brief is urgent.

```bash
sqlite3 /srv/iic-forge/data/iic.db \
  "select delivery_group_id, attempt_rank, channel, status, fallback_of from deliveries order by delivery_group_id, attempt_rank;"
```

## Focused Soak

```bash
python scripts/focused_soak_gate.py --mode soak --json
```

The focused soak passes only when source health is fresh, deferred retry is bounded, local classification calls appear in `llm_calls`, API classification spend is absent unless explicitly allowed, queue lanes are bounded, and every delivery group has success or visible failure evidence.

## Rollback

1. Stop Compose runtime:

```bash
docker compose down
```

2. Restore the previous app image or branch.
3. Restore SQLite/Redis data from `ops/backup.sh` output if the rollback needs prior state.
4. Restart Compose from the restored revision.
5. Run preflight again:

```bash
python scripts/focused_soak_gate.py --mode preflight --json
```
````

Add a short link to `README.md`:

```markdown
### Production Runtime

The canonical production runtime is Docker Compose. See `ops/runbooks/service-platform.md` for launch, rollback, Redis ownership, external local LLM, deferred retry, and focused soak procedures.
```

- [ ] **Step 4: Run green**

Run:

```bash
python -m pytest tests/ops/test_service_platform_runbook.py tests/ops/test_runtime_path_contract.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/runbooks/service-platform.md README.md tests/ops/test_service_platform_runbook.py
git commit -m "docs(ops): add service platform launch runbook"
```

---

## Phase 10 - Final Verification

### Task 14: Full focused verification pass

**Files:**
- Modify only if a prior task exposed a bug.

- [ ] **Step 1: Run targeted unit suites**

Run:

```bash
python -m pytest tests/persistence/test_platform_control_plane.py tests/llm_clients/test_llm_call_ledger.py tests/llm_clients/test_llm_call_wiring.py tests/sensing/test_deferred_salience_retry.py tests/sensing/test_source_health.py tests/delivery/test_ordered_policy.py tests/orchestrator/test_worker_lanes.py tests/dashboard/test_operations_panel.py tests/scripts/test_focused_soak_gate.py tests/ops/test_compose_contract.py tests/ops/test_runtime_path_contract.py tests/ops/test_service_platform_runbook.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader regression suites**

Run:

```bash
python -m pytest tests/persistence tests/sensing tests/orchestrator tests/delivery tests/dashboard tests/scripts tests/ops -q
```

Expected: PASS, except any socket-based tests already known to require a non-sandbox profile. If socket creation is blocked, record the failing test names and rerun them in the integration profile outside the sandbox.

- [ ] **Step 3: Render Compose config**

Run:

```bash
docker compose config >/tmp/iic-forge-compose.yml
```

Expected: exit code 0 and no `TradingAgents/TradingAgents` or `iic-redis` in `/tmp/iic-forge-compose.yml`.

- [ ] **Step 4: Run script help smoke checks**

Run:

```bash
python scripts/focused_soak_gate.py --help
python scripts/f4_f5_exit_gate.py --help
python scripts/f5_exit_gate.py --help
python scripts/shadow_eval.py --help
```

Expected: each command exits 0 and prints usage/help.

- [ ] **Step 5: Commit any verification-only fixes**

If verification required fixes:

```bash
git add <fixed-files>
git commit -m "fix(platform): resolve service reconstruction verification issues"
```

If no fixes were required, do not create an empty commit.

---

## Self-Review Checklist

**Spec coverage:**
- Canonical IIC-Forge runtime: Tasks 1, 2, 13.
- Compose-owned Redis/config/volumes/healthchecks: Tasks 1, 2, 11, 13.
- External local LLM config/probe evidence: Tasks 1, 4, 5, 10, 11, 13.
- `llm_calls` ledger: Tasks 3, 4, 5, 10, 11.
- `source_health` ledger: Tasks 3, 7, 10, 11.
- Durable deferred salience retry: Tasks 3, 6, 10, 11, 13.
- Ordered Telegram/email fallback delivery: Tasks 3, 8, 10, 11, 13.
- Worker lanes/backlog/timeout evidence: Tasks 3, 9, 10, 11.
- Dashboard and focused gate shared evidence: Tasks 10, 11.
- Old service shutdown and old path/container removal: Tasks 2, 11, 13.
- SQLite-first, no old data migration, no local LLM in Compose, no graph rewrite: enforced in baseline and task scopes.

**Placeholder scan:** This plan intentionally avoids deferred implementation placeholders. Every task names exact files, tests, commands, and code to add or adapt.

**Type/name consistency:**
- `llm_calls` helper names are `insert_llm_call` and `fetch_llm_calls`.
- Source health helper names are `upsert_source_health_success`, `upsert_source_health_failure`, and `fetch_source_health`.
- Deferred retry helper names are `insert_deferred_salience_retry`, `claim_due_deferred_salience_retries`, `reschedule_deferred_salience_retry`, `mark_deferred_salience_retry_done`, `mark_deferred_salience_retry_dead`, and `fetch_deferred_salience_retries`.
- Delivery chain fields are `delivery_group_id`, `attempt_rank`, `fallback_of`, `is_fallback`, and `failure_reason`.
- Worker lane field is `lane`; runtime config is `worker_lane`.
