# Local LLM Runbook — llama-server cutover for triage + promoter

This runbook covers the full lifecycle of running IIC triage and promoter
against a local llama-server instance instead of the upstream API: probe
commands, the cutover env flip, the model-swap procedure, and the revert.

Only the `iic-triage` (role: `triage_salience`) and `iic-promoter` (role:
`alert_gate`) units are affected. All other units — worker, action-handler,
morning, sense-* — remain on the global API provider and are not touched.

---

## 1. Probe commands

Run these before any cutover to confirm the endpoint is alive and the model
is loaded.

### 1a. Health check (llama-server convention)

```bash
BOX=127.0.0.1
PORT=8080

curl -fs http://${BOX}:${PORT}/health && echo "OK" || echo "FAIL / no /health route"
```

A `404` means this server does not expose the llama-server `/health` route
(e.g. a plain OpenAI-compatible proxy). That is a **soft-pass**: log the
warning and rely on the 1-token completion below as the sole liveness gate.
Any other non-200 (especially `503` while the model is still loading) is a
hard failure — wait and retry.

### 1b. 1-token completion (hard liveness gate)

Without an API key (default for a LAN-local server):

```bash
BOX=127.0.0.1
PORT=8080
MODEL_ID=<model-id>          # must match what llama-server reports, e.g. qwen3.6-27b-instruct-q4_k_m

curl -X POST http://${BOX}:${PORT}/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}"
```

With `LOCAL_LLM_API_KEY` set (when llama-server was started with `--api-key`):

```bash
curl -X POST http://${BOX}:${PORT}/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer ${LOCAL_LLM_API_KEY}" \
     -d "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}"
```

A `200` response with a `choices` array is a **pass**. Any transport error or
non-200 is a failure — the daemon startup probe will also fail and refuse to
start (with `fallback: none`, the default).

The IIC startup probe mirrors these two checks exactly: see
`tradingagents/llm_clients/availability.py` `probe_local_endpoint`.

---

## 2. Cutover env flip

The cutover is a pure env change — no code changes, no service file edits.
Add the following vars to the `.env` file consumed by both units
(`/opt/iic-forge/.env`, or `ops/env.iic-forge.example` as the template) or
inject them as `Environment=` lines in the service files (see the commented
blocks in `ops/systemd/iic-triage.service` and
`ops/systemd/iic-promoter.service`).

### Vars to set

```bash
# Required: the local endpoint
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1

# Optional: only when llama-server was started with --api-key
# LOCAL_LLM_API_KEY=<bearer-token>

# triage_salience role → local
IIC_TRIAGE_LLM_PROVIDER=local
IIC_TRIAGE_LLM_MODEL=<model-id>        # e.g. qwen3.6-27b-instruct-q4_k_m

# alert_gate role → local
IIC_ALERT_GATE_LLM_PROVIDER=local
IIC_ALERT_GATE_LLM_MODEL=<model-id>    # typically the same model
```

`LOCAL_LLM_BASE_URL` is consumed at request time in
`tradingagents/llm_clients/openai_client._resolve_provider_base_url`. The
four `IIC_*` vars are applied at startup in
`tradingagents/default_config._apply_nested_env_overrides`.

### Activating

```bash
# After editing .env, restart the affected Compose services:
docker compose restart triage promoter

# Verify startup probe passed (look for "startup probe OK" in each service's log):
docker compose logs --tail=20 triage   | grep -E 'probe|resolved|fallback'
docker compose logs --tail=20 promoter | grep -E 'probe|resolved|fallback'
```

The services log the resolved provider/model/base_url/fallback at startup.
With `fallback: none` (the compiled default), a dead probe refuses to start
— this is intentional: degrade loudly, not silently.

---

## 3. Fallback flip (`fallback: none` → `fallback: api`)

**Env-settable:** use `IIC_LLM_FALLBACK_MODE` for a global default across
both classification roles, or the per-role variants
`IIC_TRIAGE_LLM_FALLBACK_MODE` and `IIC_ALERT_GATE_LLM_FALLBACK_MODE`.
The daily call budget cap is `IIC_LLM_FALLBACK_DAILY_BUDGET` (global only —
no per-role budget variant exists). Note: `ops/env.iic-forge.example`
ships `IIC_LLM_FALLBACK_DAILY_BUDGET=0`, which overrides the compiled
default of 500 — if you enable fallback, raise the budget too or zero
fallback calls will be permitted.

```bash
# In .env — flip triage and promoter to fallback=api:
IIC_TRIAGE_LLM_FALLBACK_MODE=api
IIC_ALERT_GATE_LLM_FALLBACK_MODE=api
```

Then restart:

```bash
docker compose restart triage promoter
```

Alternatively, to flip a role by editing the compiled default (a code change
+ redeploy), change the `"fallback": "none"` entry in
`tradingagents/default_config.py` for the relevant role.

When `fallback: "api"` is active, a dead startup probe or a consecutive
runtime failure run that reaches `fallback_threshold` (default: 3) causes
the role to re-resolve to the global provider. The daily fallback budget
caps the API spend (compiled default 500 calls/UTC-day; the env template
ships `IIC_LLM_FALLBACK_DAILY_BUDGET=0`, which takes precedence). Budget
consumption is persisted in `ops_counters` and survives restarts.

The `fallback: "none"` default is the **recommended mode for production**:
it prevents silent API spend when the local endpoint is unexpectedly down,
and the self-alert (section 6 below) notifies the operator instead.

---

## 4. Model-swap procedure

A model swap replaces the GGUF file and restarts llama-server. **No IIC code
change is required.** The model ID passed in `IIC_TRIAGE_LLM_MODEL` /
`IIC_ALERT_GATE_LLM_MODEL` must match the model identifier llama-server
reports; update the `.env` entries when the ID changes.

### Never hot-swap

Do not replace the GGUF while llama-server is serving requests. Always stop
the server, replace the file, then start the server. Hot-swapping the file
under a running process corrupts in-flight requests and can crash the daemon.

### Swap procedure

```bash
# 1. Stop IIC Compose services that use the local endpoint
docker compose stop triage promoter

# 2. Stop llama-server
sudo systemctl stop llama-server   # adjust unit name to your install

# 3. Replace the GGUF
mv /path/to/models/<old-model>.gguf /path/to/models/<old-model>.gguf.bak
cp /path/to/new/<new-model>.gguf /path/to/models/

# 4. Update the model ID in .env
#    IIC_TRIAGE_LLM_MODEL=<new-model-id>
#    IIC_ALERT_GATE_LLM_MODEL=<new-model-id>

# 5. Start llama-server with the new model
sudo systemctl start llama-server

# 6. Verify the endpoint is alive (probe commands from section 1)
curl -fs http://127.0.0.1:8080/health
```

### Swap gate — tests that must pass before re-enabling IIC daemons

Run both gates in sequence. **Both must pass** before restarting triage and
promoter. Do not skip. Do not hot-swap and gate in parallel.

**L0 contract tests** (unit-level, no live endpoint required):

```bash
python -m pytest tests/llm_clients/test_local_contract.py \
                 tests/llm_clients/test_json_schema_binding.py -q
```

**L2 shadow-eval harness** (requires the new endpoint to be live):

```bash
python scripts/shadow_eval.py \
    --limit 500 \
    --model <new-model-id> \
    --persist-set
```

L2 acceptance thresholds (all must pass):

| Gate | Threshold |
|---|---|
| Salience threshold-crossing agreement (@0.85) | ≥ 95 % |
| Evaluator verdict agreement (@0.9) | ≥ 90 % |
| Cohen's κ | reported (no minimum, but record it) |
| Parse failures | = 0 |
| Local p95 latency | ≤ API p95 latency |

Once both gates pass, restart the Compose services:

```bash
docker compose start triage promoter
```

### Post-restart monitoring (soak-report counters)

`--soak-report` counters accrue only after the daemons have restarted on the
new model. Check them during and after the monitoring window:

```bash
cd /opt/iic-forge && \
python scripts/f4_f5_exit_gate.py \
    --soak-report --local-model-id <new-model-id>
# Add --json for machine-readable output
```

**Initial model qualification (first-ever cutover):** run a full 24 h shadow
soak before running the L2 replay gate. The soak-report is the acceptance
record for this qualification step. Routine same-family quant swaps (e.g.
`q4_k_m` → `q5_k_m`) use the L0 + L2 replay gate only; the 24 h shadow soak
is replaced by post-restart monitoring.

---

## 5. Revert (unset all four IIC_* vars)

The revert is a pure env unset — no code changes required.

Remove (or comment out) **all four** `IIC_*` vars from `.env`:

```bash
# Remove or comment out ALL FOUR:
# IIC_TRIAGE_LLM_PROVIDER=local
# IIC_TRIAGE_LLM_MODEL=<model-id>
# IIC_ALERT_GATE_LLM_PROVIDER=local
# IIC_ALERT_GATE_LLM_MODEL=<model-id>
```

**Why all four?** `create_role_llm` resolves provider and model independently.
Leaving `IIC_TRIAGE_LLM_MODEL` or `IIC_ALERT_GATE_LLM_MODEL` set while
unsetting only the provider yields `provider=deepseek` + `model=qwen…`,
which produces an API model-not-found error on every call. No warning fires
for this combination (the RuntimeWarning covers only provider-without-model
in the opposite direction).

`LOCAL_LLM_BASE_URL` and `LOCAL_LLM_API_KEY` are **safe to leave set** —
they are only consumed when `provider=local` is active.

Then reload:

```bash
docker compose restart triage promoter

# Confirm roles resolved to the API provider:
docker compose logs --tail=10 triage   | grep 'resolved:'
docker compose logs --tail=10 promoter | grep 'resolved:'
```

---

## 6. Soak verification and observability

### Soak report

```bash
cd /opt/iic-forge && \
python scripts/f4_f5_exit_gate.py \
    --soak-report [--local-model-id <id>] [--json]
```

### Ops counters (failure / fallback budgets)

Counters live in the `ops_counters` table of the IIC SQLite database.
Relevant counter names:

| Counter | What it measures |
|---|---|
| `triage_llm_failures` | Monotonic per-event salience failures (including parse errors) |
| `promoter_llm_failures` | Monotonic per-cycle transport failures |
| `triage_fallback_calls:<YYYY-MM-DD>` | Daily fallback-to-API calls for triage (only when fallback=api) |
| `promoter_fallback_calls:<YYYY-MM-DD>` | Daily fallback-to-API calls for promoter (only when fallback=api) |

```bash
sqlite3 /srv/iic-forge/data/iic.db \
    "SELECT name, value FROM ops_counters WHERE name LIKE '%llm%' OR name LIKE '%fallback%' ORDER BY name"
```

### Dashboard costs tab

The dashboard (`http://127.0.0.1:8501`) costs tab shows the local/API spend
split. A healthy cutover shows API cost drop and local cost at zero (local
calls are not billed).

### Self-alert: "local LLM endpoint down"

When consecutive failures reach `fallback_threshold` (default 3), the daemon
fires a one-shot self-alert via:

- **Telegram**: `IIC_TELEGRAM_BOT_TOKEN` + `telegram_bot` config (see
  `tradingagents/ops/self_alert.py`). The alert is sent once per outage and
  re-armed on the next successful LLM call.
- **Log**: always emitted regardless of Telegram config; visible via:
  ```bash
  journalctl -u iic-triage --no-pager | grep 'SELF-ALERT'
  journalctl -u iic-promoter --no-pager | grep 'SELF-ALERT'
  ```
  (Python logging does not emit sd-priority prefixes, so journald records
  stderr at `info` priority; `-p crit` returns nothing.)

The alert is debounced: one alert per outage, not one per failure. A
recovered endpoint re-arms the latch so the next outage alerts again.
