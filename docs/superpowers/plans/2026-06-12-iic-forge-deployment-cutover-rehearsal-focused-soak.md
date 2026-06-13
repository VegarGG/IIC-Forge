# IIC-Forge Deployment Cutover Rehearsal And Focused Soak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rehearse the Compose production cutover on the production host, capture evidence for each launch invariant, then run the focused soak gate over live operational state before declaring the service platform ready.

**Architecture:** No application code changes are expected. The plan treats `ops/runbooks/service-platform.md` as the canonical operator runbook, `compose.yml` as the runtime source of truth, and `scripts/focused_soak_gate.py` plus the dashboard Operations query layer as the shared evidence contract. Every task either prepares host state, starts or verifies Compose-owned services, captures rollback evidence, or evaluates the focused soak gate.

**Tech Stack:** Docker Compose, systemd, Redis 7, SQLite/WAL, Python 3, `scripts/focused_soak_gate.py`, `ops/backup.sh`, IIC-Forge production host paths (`/opt/iic-forge`, `/srv/iic-forge/data`, `/srv/iic-forge/backups`).

**Spec:** `docs/superpowers/specs/2026-06-12-iic-forge-service-platform-reconstruction-design.md`

**Runbook:** `ops/runbooks/service-platform.md`

**Execution Host:** production host, repo root `/opt/iic-forge`

---

## File Structure

**Repository files read during execution:**
- `compose.yml` - Compose runtime and service/profile contract.
- `ops/env.iic-forge.example` - committed env template copied to private `.env`.
- `ops/runbooks/service-platform.md` - launch, rollback, backup, Redis, local LLM, and soak procedures.
- `ops/backup.sh` - SQLite and Redis backup script.
- `scripts/focused_soak_gate.py` - focused preflight and soak gate.
- `tradingagents/dashboard/panels/operations.py` - shared operational evidence query layer.

**Host files/directories created during execution:**
- `/opt/iic-forge/.env` - private operator environment file, never committed.
- `/srv/iic-forge/data/` - bind-backed application data directory used by Compose volume `iic_data`.
- `/srv/iic-forge/backups/` - backup root used by `ops/backup.sh`.
- `/srv/iic-forge/cutover-evidence/${STAMP}/` - command outputs and JSON reports for this rehearsal and soak; `${STAMP}` is created in Task 1.

**Repository files modified by this plan:** none. If a command uncovers a bug that requires a code or docs change, stop this plan, write a targeted follow-up plan or patch, and do not continue the production cutover in the same run.

---

## Preconditions

- The 38 reconstruction commits are available on the production host as the intended release revision.
- The working tree on the production host is clean before launch.
- Docker and the Docker Compose plugin are installed.
- The local LLM server is already running outside Compose and exposes an OpenAI-compatible endpoint reachable as `http://host.docker.internal:8080/v1` from containers.
- Operator secrets are available out of band for `.env`: Telegram bot token, Telegram sensing credentials, allowed chat IDs, Polygon key, FRED key if macro polling is enabled, SMTP settings if email fallback is enabled, and optional API-provider keys for deep studies or deliberate fallback.
- Old `TradingAgents` runtime services can be disabled on the host during the rehearsal window.

---

## Task 1: Freeze Release Revision And Evidence Directory

**Files:**
- Read: `compose.yml`
- Read: `ops/runbooks/service-platform.md`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/release.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/git-status.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/git-log.txt`

- [ ] **Step 1: Enter the production repo**

Run on the production host:

```bash
cd /opt/iic-forge
```

Expected: command exits `0`.

- [ ] **Step 2: Create an evidence directory**

Run:

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="/srv/iic-forge/cutover-evidence/${STAMP}"
mkdir -p "${EVIDENCE_DIR}"
printf '%s\n' "${STAMP}" > "${EVIDENCE_DIR}/stamp.txt"
printf '%s\n' "${EVIDENCE_DIR}" > /tmp/iic-forge-cutover-evidence-dir
```

Expected: command exits `0`; `/tmp/iic-forge-cutover-evidence-dir` contains the evidence path for later steps.

- [ ] **Step 3: Capture release identity**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
{
  echo "branch=$(git branch --show-current)"
  echo "head=$(git rev-parse HEAD)"
  echo "origin_claude_impl_ahead=$(git rev-list --count origin/claude/iic-forge-05-impl..HEAD 2>/dev/null || echo unknown)"
  echo "origin_main_ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo unknown)"
} > "${EVIDENCE_DIR}/release.txt"
git status --short --branch > "${EVIDENCE_DIR}/git-status.txt"
git log --oneline --decorate -n 45 > "${EVIDENCE_DIR}/git-log.txt"
cat "${EVIDENCE_DIR}/release.txt"
cat "${EVIDENCE_DIR}/git-status.txt"
```

Expected:
- `git-status.txt` begins with the intended release branch.
- No uncommitted files appear after the branch line.
- `release.txt` records the exact `HEAD`.
- If the production host uses a release tag rather than a branch, record that tag in `release.txt` before continuing.

- [ ] **Step 4: Stop on branch/ref mismatch**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
if grep -qE '^\?\?|^ M|^M |^ A|^A |^ D|^D ' "${EVIDENCE_DIR}/git-status.txt"; then
  echo "dirty working tree; stop cutover"
  exit 1
fi
```

Expected: command exits `0`. If it exits `1`, cleanly stop the cutover rehearsal and resolve the dirty tree before continuing.

---

## Task 2: Prepare Host Directories And Private Environment

**Files:**
- Read: `ops/env.iic-forge.example`
- Create/Modify: `/opt/iic-forge/.env`
- Create: `/srv/iic-forge/data/`
- Create: `/srv/iic-forge/backups/`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/env-audit.txt`

- [ ] **Step 1: Create production data and backup directories**

Run:

```bash
sudo mkdir -p /srv/iic-forge/data /srv/iic-forge/backups /srv/iic-forge/cutover-evidence
sudo chown "$USER:$USER" /srv/iic-forge/data /srv/iic-forge/backups /srv/iic-forge/cutover-evidence
```

Expected: command exits `0`.

- [ ] **Step 2: Create private `.env` if absent**

Run:

```bash
if [ ! -f .env ]; then
  cp ops/env.iic-forge.example .env
  chmod 600 .env
fi
ls -l .env
```

Expected: `.env` exists and is private to the operator account (`-rw-------` or stricter).

- [ ] **Step 3: Edit `.env` with production values**

Run:

```bash
"${EDITOR:-vi}" .env
```

Expected production values:

```dotenv
TRADINGAGENTS_IIC_DB_PATH=/data/iic.db
TRADINGAGENTS_IIC_DATA_DIR=/data
TRADINGAGENTS_SENSING_REDIS_URL=redis://redis:6379/0
LOCAL_LLM_BASE_URL=http://host.docker.internal:8080/v1
IIC_TRIAGE_LLM_PROVIDER=local
IIC_ALERT_GATE_LLM_PROVIDER=local
IIC_LLM_FALLBACK_MODE=none
IIC_LLM_FALLBACK_DAILY_BUDGET=0
IIC_DELIVERY_POLICY=ordered_telegram_email
IIC_WORKER_DEEP_CONCURRENCY=1
IIC_SOURCE_STALE_AFTER_SECONDS=1800
IIC_DEFERRED_RETRY_MAX_PENDING=100
IIC_DELIVERY_FAILED_GROUP_MAX=0
IIC_ALLOW_API_CLASSIFICATION_SPEND=false
```

Expected secret-bearing variables are set when their services are enabled. Do not paste secret values into this plan or into evidence files; the audit in Step 4 records only `SET` or `EMPTY` for secret-like keys.

```dotenv
IIC_TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_ALLOWED_CHAT_IDS=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SENSING_CHANNELS=
POLYGON_API_KEY=
FRED_API_KEY=
IIC_SMTP_ENABLED=false
IIC_SMTP_USER=
IIC_SMTP_APP_PASSWORD=
IIC_SMTP_TO_ADDRS=
DEEPSEEK_API_KEY=
```

Required interpretation:
- `IIC_TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_ALLOWED_CHAT_IDS`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SENSING_CHANNELS` must be populated when Telegram delivery or Telegram sensing is enabled.
- `POLYGON_API_KEY` must be populated when the Polygon adapter is enabled.
- `FRED_API_KEY` must be populated when the macro adapter is enabled; otherwise disable macro before treating source freshness as meaningful.
- `IIC_SMTP_USER`, `IIC_SMTP_APP_PASSWORD`, and `IIC_SMTP_TO_ADDRS` must be populated when `IIC_SMTP_ENABLED=true`.
- `DEEPSEEK_API_KEY` must be populated when deep studies or deliberate API fallback are expected.

- [ ] **Step 4: Audit `.env` without printing secrets**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
python - <<'PY' > "${EVIDENCE_DIR}/env-audit.txt"
from pathlib import Path

required = [
    "TRADINGAGENTS_IIC_DB_PATH",
    "TRADINGAGENTS_IIC_DATA_DIR",
    "TRADINGAGENTS_SENSING_REDIS_URL",
    "LOCAL_LLM_BASE_URL",
    "IIC_TRIAGE_LLM_PROVIDER",
    "IIC_ALERT_GATE_LLM_PROVIDER",
    "IIC_LLM_FALLBACK_MODE",
    "IIC_DELIVERY_POLICY",
    "IIC_SOURCE_STALE_AFTER_SECONDS",
    "IIC_DEFERRED_RETRY_MAX_PENDING",
    "IIC_DELIVERY_FAILED_GROUP_MAX",
    "IIC_ALLOW_API_CLASSIFICATION_SPEND",
]
secretish = ("TOKEN", "KEY", "HASH", "PASSWORD", "SECRET")
values = {}
for line in Path(".env").read_text().splitlines():
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

missing = [key for key in required if key not in values or values[key] == ""]
for key in sorted(values):
    value = values[key]
    if any(word in key for word in secretish):
        shown = "SET" if value else "EMPTY"
    elif key in {"TELEGRAM_BOT_ALLOWED_CHAT_IDS", "TELEGRAM_SENSING_CHANNELS", "IIC_SMTP_TO_ADDRS"}:
        shown = f"{len([x for x in value.split(',') if x.strip()])} item(s)"
    else:
        shown = value
    print(f"{key}={shown}")
print(f"missing_required={missing}")
raise SystemExit(1 if missing else 0)
PY
cat "${EVIDENCE_DIR}/env-audit.txt"
```

Expected:
- Command exits `0`.
- `missing_required=[]`.
- No secret values are printed; secret-like keys display only `SET` or `EMPTY`.

---

## Task 3: Render Compose Contract And Build Runtime Image

**Files:**
- Read: `compose.yml`
- Read: `.env`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/compose-rendered.yml`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/compose-build.txt`

- [ ] **Step 1: Render the full production Compose profile**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose --profile runtime --profile sources --profile dashboard --profile gate --profile x config > "${EVIDENCE_DIR}/compose-rendered.yml"
```

Expected: command exits `0`.

- [ ] **Step 2: Verify rendered service and env seams**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
python - <<'PY'
from pathlib import Path
import sys
import yaml

path = Path("/tmp/iic-forge-cutover-evidence-dir").read_text().strip()
data = yaml.safe_load((Path(path) / "compose-rendered.yml").read_text())
services = data["services"]
expected = {
    "redis", "adapter-polygon", "adapter-telegram", "adapter-rss",
    "adapter-gdelt", "adapter-macro", "triage", "promoter",
    "worker-action", "worker-deep", "action-handler", "delivery", "dashboard",
}
missing = sorted(expected - set(services))
bad = []
for service_name in ("triage", "promoter", "adapter-gdelt", "adapter-macro"):
    env = services[service_name].get("environment", {})
    if env.get("TRADINGAGENTS_SENSING_REDIS_URL") != "redis://redis:6379/0":
        bad.append(f"{service_name}:redis_url={env.get('TRADINGAGENTS_SENSING_REDIS_URL')!r}")
if "TradingAgents/TradingAgents" in (Path(path) / "compose-rendered.yml").read_text():
    bad.append("old TradingAgents path present")
if "iic-redis" in (Path(path) / "compose-rendered.yml").read_text():
    bad.append("legacy iic-redis reference present")
if missing or bad:
    print({"missing": missing, "bad": bad})
    sys.exit(1)
print("compose contract ok")
PY
```

Expected: prints `compose contract ok` and exits `0`.

- [ ] **Step 3: Build the runtime image**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose --profile runtime --profile sources --profile dashboard build 2>&1 | tee "${EVIDENCE_DIR}/compose-build.txt"
```

Expected:
- Command exits `0`.
- `compose-build.txt` contains no failed build step.

---

## Task 4: Prove Backup And Rollback Inputs Before Cutover

**Files:**
- Read: `ops/backup.sh`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/backup-before-cutover.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/backup-inventory.txt`
- Create: `/srv/iic-forge/backups/${BACKUP_STAMP}/iic.db`
- Create: `/srv/iic-forge/backups/${BACKUP_STAMP}/redis-dump.rdb`

- [ ] **Step 1: Start Redis only for backup smoke if the stack is not running**

Run:

```bash
docker compose up -d redis
docker compose ps redis
```

Expected: Redis service is `running` or `healthy`.

- [ ] **Step 2: Run backup script before disabling old services**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
bash ops/backup.sh 2>&1 | tee "${EVIDENCE_DIR}/backup-before-cutover.txt"
```

Expected:
- Command exits `0`.
- Output includes `backup written to /srv/iic-forge/backups/` followed by the backup timestamp directory.

- [ ] **Step 3: Record backup inventory**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
BACKUP_DIR="$(awk '/backup written to / {print $4}' "${EVIDENCE_DIR}/backup-before-cutover.txt" | tail -n 1)"
test -n "${BACKUP_DIR}"
test -f "${BACKUP_DIR}/iic.db"
test -f "${BACKUP_DIR}/redis-dump.rdb"
find "${BACKUP_DIR}" -maxdepth 1 -type f -printf '%f %s bytes\n' | sort > "${EVIDENCE_DIR}/backup-inventory.txt"
cat "${EVIDENCE_DIR}/backup-inventory.txt"
```

Expected:
- Command exits `0`.
- Inventory lists `iic.db` and `redis-dump.rdb`, both with nonzero byte sizes.

- [ ] **Step 4: Record rollback revision**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
git rev-parse HEAD > "${EVIDENCE_DIR}/rollback-current-head.txt"
git rev-parse origin/main > "${EVIDENCE_DIR}/rollback-origin-main.txt"
cat "${EVIDENCE_DIR}/rollback-current-head.txt"
cat "${EVIDENCE_DIR}/rollback-origin-main.txt"
```

Expected: both files contain valid commit SHAs. If rollback should target a different previous release SHA, write that SHA to `${EVIDENCE_DIR}/rollback-target.txt` before continuing.

---

## Task 5: Disable Legacy Runtime And Launch Compose Runtime

**Files:**
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/legacy-disable.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/legacy-active-after-disable.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/compose-up.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/compose-ps-after-up.txt`

- [ ] **Step 1: Disable old per-daemon services**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
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
  2>&1 | tee "${EVIDENCE_DIR}/legacy-disable.txt" || true
```

Expected: command completes. `not loaded` lines are acceptable for services that were never installed.

- [ ] **Step 2: Confirm old services are inactive**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
systemctl is-active \
  iic-action-handler.service iic-dashboard.service iic-morning.service \
  iic-morning.timer iic-promoter.service iic-sense-gdelt.service \
  iic-sense-macro.service iic-sense-polygon.service iic-sense-rss.service \
  iic-sense-telegram.service iic-sense-x.service iic-telegram-bot.service \
  iic-triage.service iic-watchlist-sweep.service iic-watchlist-sweep.timer \
  iic-worker.service redis-server.service \
  > "${EVIDENCE_DIR}/legacy-active-after-disable.txt" || true
cat "${EVIDENCE_DIR}/legacy-active-after-disable.txt"
if grep -q '^active$' "${EVIDENCE_DIR}/legacy-active-after-disable.txt"; then
  echo "at least one legacy unit is still active; stop cutover"
  exit 1
fi
```

Expected: command exits `0`; no line is exactly `active`.

- [ ] **Step 3: Launch Compose runtime**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose --profile runtime --profile sources --profile dashboard up -d --build 2>&1 | tee "${EVIDENCE_DIR}/compose-up.txt"
docker compose ps > "${EVIDENCE_DIR}/compose-ps-after-up.txt"
cat "${EVIDENCE_DIR}/compose-ps-after-up.txt"
```

Expected:
- Command exits `0`.
- `compose-ps-after-up.txt` lists `redis`, source adapters, `triage`, `promoter`, `worker-deep`, `worker-action`, `action-handler`, `delivery`, and `dashboard`.
- `redis` is healthy.

- [ ] **Step 4: Capture immediate runtime logs**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose logs --tail=120 redis triage promoter worker-deep worker-action action-handler delivery dashboard > "${EVIDENCE_DIR}/compose-logs-initial.txt"
tail -n 80 "${EVIDENCE_DIR}/compose-logs-initial.txt"
```

Expected:
- No repeated crash loop is visible.
- `worker-action` may be idle by design.
- If `triage` or `promoter` refuses startup due to local LLM unavailability, stop the cutover and fix the external local LLM before continuing.

---

## Task 6: Run Host Preflight Gate And Redis Ownership Proof

**Files:**
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/preflight-gate.json`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/redis-proof.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/dashboard-health.txt`

- [ ] **Step 1: Run focused gate in preflight mode on the host**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
set -a; . ./.env; set +a
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode preflight --json \
  > "${EVIDENCE_DIR}/preflight-gate.json"
python - <<'PY'
from pathlib import Path
import json

evidence_dir = Path("/tmp/iic-forge-cutover-evidence-dir").read_text().strip()
report = json.loads((Path(evidence_dir) / "preflight-gate.json").read_text())
print("pass=", report["pass"])
for name, check in report["checks"].items():
    print(name, check["pass"], check["detail"])
raise SystemExit(0 if report["pass"] else 1)
PY
```

Expected:
- Command exits `0`.
- `old_services_stopped`, `redis_owned_and_configured`, `deferred_retry_bounded`, `llm_failures_bounded`, `no_unexpected_api_classification_spend`, and `delivery_groups_bounded` pass.
- `sources_fresh` and `llm_calls_present` are marked pass with `skipped in preflight mode`.

- [ ] **Step 2: Capture Redis config proof**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
{
  docker compose exec -T redis redis-cli ping
  docker compose exec -T redis redis-cli CONFIG GET appendonly
  docker compose exec -T redis redis-cli CONFIG GET maxmemory-policy
} > "${EVIDENCE_DIR}/redis-proof.txt"
cat "${EVIDENCE_DIR}/redis-proof.txt"
```

Expected:
- Output includes `PONG`.
- Output includes `appendonly` followed by `yes`.
- Output includes `maxmemory-policy` followed by `noeviction`.

- [ ] **Step 3: Capture dashboard health**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
python - <<'PY' > "${EVIDENCE_DIR}/dashboard-health.txt"
import urllib.request

body = urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=5).read()
print(body.decode("utf-8", errors="replace"))
PY
cat "${EVIDENCE_DIR}/dashboard-health.txt"
```

Expected: command exits `0`; dashboard health endpoint returns a non-error response.

---

## Task 7: Drive First Live Evidence Cycle

**Files:**
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/source-health-after-wait.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/llm-calls-after-wait.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/delivery-groups-after-wait.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/queue-lanes-after-wait.txt`

- [ ] **Step 1: Wait for source poll and triage cycles**

Run:

```bash
sleep 600
```

Expected: command exits after 10 minutes. This gives 15-minute-style pollers partial time to start and faster sources time to record health; extend to 1800 seconds if the first check in Step 2 shows missing source-health rows.

- [ ] **Step 2: Inspect source health**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('''
  SELECT source, service_name, last_poll_ts, last_success_ts,
         events_emitted_total, events_emitted_last_poll,
         consecutive_failures, substr(coalesce(last_error, ''), 1, 160)
  FROM source_health
  ORDER BY source
''')
for row in c.fetchall():
    print(row)
" > "${EVIDENCE_DIR}/source-health-after-wait.txt"
cat "${EVIDENCE_DIR}/source-health-after-wait.txt"
```

Expected:
- Enabled sources have rows.
- `consecutive_failures` is `0` for sources expected to be healthy.
- If `macro` fails because `FRED_API_KEY` is intentionally empty, either set the key or disable the macro adapter before treating the soak as meaningful.

- [ ] **Step 3: Inspect classification LLM calls**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('''
  SELECT role, provider, model_id, status, parse_ok, fallback_used, COUNT(*)
  FROM llm_calls
  WHERE role IN ('triage_salience', 'alert_gate', 'light_alert_summary')
  GROUP BY role, provider, model_id, status, parse_ok, fallback_used
  ORDER BY role, provider, status
''')
for row in c.fetchall():
    print(row)
" > "${EVIDENCE_DIR}/llm-calls-after-wait.txt"
cat "${EVIDENCE_DIR}/llm-calls-after-wait.txt"
```

Expected:
- At least one `triage_salience` or `alert_gate` row appears before focused soak is accepted.
- Provider is `local` for classification roles unless deliberate API fallback was explicitly allowed.
- No `parse_error` or `transport_error` rows appear during a clean soak window.

- [ ] **Step 4: Inspect delivery and queue evidence**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('''
  SELECT delivery_group_id, attempt_rank, channel, status, skip_reason,
         fallback_of, is_fallback
  FROM deliveries
  ORDER BY delivery_group_id, attempt_rank
  LIMIT 50
''')
for row in c.fetchall():
    print(row)
" > "${EVIDENCE_DIR}/delivery-groups-after-wait.txt"
docker compose run --rm --entrypoint python triage -c "
import sqlite3
c = sqlite3.connect('/data/iic.db').cursor()
c.execute('SELECT lane, state, COUNT(*) FROM queue_jobs GROUP BY lane, state ORDER BY lane, state')
for row in c.fetchall():
    print(row)
" > "${EVIDENCE_DIR}/queue-lanes-after-wait.txt"
cat "${EVIDENCE_DIR}/delivery-groups-after-wait.txt"
cat "${EVIDENCE_DIR}/queue-lanes-after-wait.txt"
```

Expected:
- Delivery groups are empty or have auditable sent/skipped/fallback attempts.
- Failed delivery groups must be investigated before soak acceptance.
- `worker-action` may have no rows; that is expected at launch.
- `worker-deep` can have zero rows until a full study is approved.

---

## Task 8: Run Focused Production Soak Gate

**Files:**
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/focused-soak-gate.json`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/focused-soak-summary.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/operations-snapshot-summary.txt`

- [ ] **Step 1: Run the soak gate**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
set -a; . ./.env; set +a
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode soak --json \
  > "${EVIDENCE_DIR}/focused-soak-gate.json"
python - <<'PY' > "${EVIDENCE_DIR}/focused-soak-summary.txt"
from pathlib import Path
import json

evidence_dir = Path("/tmp/iic-forge-cutover-evidence-dir").read_text().strip()
report = json.loads((Path(evidence_dir) / "focused-soak-gate.json").read_text())
print(f"pass={report['pass']}")
for name, check in report["checks"].items():
    print(f"{name}: pass={check['pass']} detail={check['detail']}")
raise SystemExit(0 if report["pass"] else 1)
PY
cat "${EVIDENCE_DIR}/focused-soak-summary.txt"
```

Expected: command exits `0` only when all 8 focused soak checks pass.

- [ ] **Step 2: Capture compact operations snapshot**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
python - <<'PY' > "${EVIDENCE_DIR}/operations-snapshot-summary.txt"
from pathlib import Path
import json

evidence_dir = Path("/tmp/iic-forge-cutover-evidence-dir").read_text().strip()
report = json.loads((Path(evidence_dir) / "focused-soak-gate.json").read_text())
snap = report["snapshot"]
print("sources=", sorted(snap["sources"]))
print("llm_roles=", sorted(snap["llm_calls"]))
print("deferred=", snap["deferred_salience"])
print("delivery_groups=", snap["delivery_groups"])
print("queue_lanes=", snap["queue_lanes"])
print("costs=", snap["costs"])
PY
cat "${EVIDENCE_DIR}/operations-snapshot-summary.txt"
```

Expected: summary matches the gate result and is small enough to paste into the deployment record.

- [ ] **Step 3: Classify failures if the gate fails**

Run only if Step 1 exits nonzero:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
python - <<'PY'
from pathlib import Path
import json

evidence_dir = Path("/tmp/iic-forge-cutover-evidence-dir").read_text().strip()
report = json.loads((Path(evidence_dir) / "focused-soak-gate.json").read_text())
for name, check in report["checks"].items():
    if not check["pass"]:
        print(f"FAILED {name}: {check['detail']}")
PY
```

Expected failure actions:
- `old_services_stopped`: rerun Task 5 Step 1 and confirm no legacy units are active.
- `redis_owned_and_configured`: inspect `docker compose ps redis` and Redis config mount before continuing.
- `sources_fresh`: inspect source logs and source credentials; do not accept soak with missing required sources.
- `deferred_retry_bounded`: inspect local LLM health and retry rows; wait for recovery only if pending rows are actively draining.
- `llm_calls_present`: generate a known live event path or wait for the next source cycle; do not accept soak without classification evidence.
- `llm_failures_bounded`: fix parse/transport errors before accepting soak.
- `no_unexpected_api_classification_spend`: confirm fallback policy; do not accept accidental API classification spend.
- `delivery_groups_bounded`: fix Telegram/email configuration or delivery provider failures before accepting soak.

---

## Task 9: Decide Cutover Acceptance Or Rollback

**Files:**
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/cutover-decision.txt`
- Read: `/srv/iic-forge/cutover-evidence/${STAMP}/focused-soak-summary.txt`
- Read: `/srv/iic-forge/cutover-evidence/${STAMP}/backup-inventory.txt`

- [ ] **Step 1: Write acceptance decision when focused soak passes**

Run only if Task 8 Step 1 passed:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
{
  echo "decision=accept"
  echo "decided_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "release_head=$(git rev-parse HEAD)"
  echo "evidence_dir=${EVIDENCE_DIR}"
  echo "focused_soak_summary:"
  cat "${EVIDENCE_DIR}/focused-soak-summary.txt"
} > "${EVIDENCE_DIR}/cutover-decision.txt"
cat "${EVIDENCE_DIR}/cutover-decision.txt"
```

Expected: decision file records `decision=accept`, release head, evidence directory, and focused soak summary.

- [ ] **Step 2: Execute rollback when a launch blocker remains**

Run only if a launch blocker remains after one remediation attempt:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
{
  echo "decision=rollback"
  echo "decided_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "release_head=$(git rev-parse HEAD)"
  echo "reason=focused soak or preflight blocker remained after remediation"
  echo "evidence_dir=${EVIDENCE_DIR}"
} > "${EVIDENCE_DIR}/cutover-decision.txt"
docker compose down
ROLLBACK_TARGET="$(cat "${EVIDENCE_DIR}/rollback-target.txt" 2>/dev/null || cat "${EVIDENCE_DIR}/rollback-origin-main.txt")"
git checkout "${ROLLBACK_TARGET}"
docker compose --profile runtime --profile sources --profile dashboard up -d --build
set -a; . ./.env; set +a
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode preflight --json \
  > "${EVIDENCE_DIR}/rollback-preflight-gate.json"
cat "${EVIDENCE_DIR}/cutover-decision.txt"
```

Expected:
- Compose stops before rollback checkout.
- Rollback checkout succeeds.
- Rolled-back Compose runtime starts.
- `rollback-preflight-gate.json` is captured for follow-up triage.

---

## Task 10: Post-Cutover Watch Window

**Files:**
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/watch-window-1h.txt`
- Create: `/srv/iic-forge/cutover-evidence/${STAMP}/watch-window-gate-1h.json`

- [ ] **Step 1: Watch logs for one hour after acceptance**

Run after Task 9 Step 1:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
{
  echo "watch_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  docker compose ps
  docker compose logs --since=1h redis triage promoter worker-deep worker-action action-handler delivery dashboard
  echo "watch_finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${EVIDENCE_DIR}/watch-window-1h.txt"
```

Expected:
- No restart storm.
- No repeated local LLM transport failure.
- No growing deferred retry backlog.
- `worker-action` remains idle unless a future action-lane producer has been added.

- [ ] **Step 2: Re-run focused gate after the watch window**

Run:

```bash
EVIDENCE_DIR="$(cat /tmp/iic-forge-cutover-evidence-dir)"
set -a; . ./.env; set +a
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db \
  python scripts/focused_soak_gate.py --mode soak --json \
  > "${EVIDENCE_DIR}/watch-window-gate-1h.json"
python - <<'PY'
from pathlib import Path
import json

evidence_dir = Path("/tmp/iic-forge-cutover-evidence-dir").read_text().strip()
report = json.loads((Path(evidence_dir) / "watch-window-gate-1h.json").read_text())
print("pass=", report["pass"])
for name, check in report["checks"].items():
    print(name, check["pass"], check["detail"])
raise SystemExit(0 if report["pass"] else 1)
PY
```

Expected: command exits `0`. If it fails after initial acceptance, open a production incident note and decide whether to rollback using Task 9 Step 2.

---

## Self-Review

**Spec coverage:**
- Compose-owned production runtime: Tasks 3, 5, 6.
- Old runtime shutdown: Task 5.
- IIC-owned Redis with loaded config: Tasks 3, 6.
- External local LLM evidence: Tasks 2, 5, 7, 8.
- Durable deferred retry and shared operational evidence: Tasks 6, 7, 8, 10.
- Ordered delivery evidence: Tasks 7, 8.
- Dashboard/gate shared evidence: Tasks 6, 8, 10.
- Rollback readiness: Tasks 4, 9.

**Placeholder scan:** This plan intentionally avoids incomplete placeholder instructions. Host-specific variable values are produced by exact commands (`STAMP`, `EVIDENCE_DIR`, `BACKUP_DIR`) and recorded in evidence files.

**Boundary notes:**
- `worker-action` is expected to idle at launch because no producer routes `lane=action`.
- Effective worker concurrency remains one job per worker process until a future multi-slot worker loop exists.
- `llm_calls` focused soak acceptance covers classification roles through `triage_salience` and `alert_gate`; graph/deep-study cost evidence remains in existing cost tables.
- Production soak cannot be accepted from inside the `gate-runner` container because `--skip-host-probes` skips old-service and Redis host probes. Acceptance must use the host-run command in Task 8.
