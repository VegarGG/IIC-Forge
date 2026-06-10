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
MODEL_ID=<model-id>          # must match what llama-server reports, e.g. qwen3.6-27b-q4_k_m

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
(`/home/ziwei-huang/TradingAgents/TradingAgents/.env`) or inject them as
`Environment=` lines in the service files (see the commented blocks in
`ops/systemd/iic-triage.service` and `ops/systemd/iic-promoter.service`).

### Vars to set

```bash
# Required: the local endpoint
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1

# Optional: only when llama-server was started with --api-key
# LOCAL_LLM_API_KEY=<bearer-token>

# triage_salience role → local
IIC_TRIAGE_LLM_PROVIDER=local
IIC_TRIAGE_LLM_MODEL=<model-id>        # e.g. qwen3.6-27b-q4_k_m

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
# After editing .env:
sudo systemctl daemon-reload
sudo systemctl restart iic-triage iic-promoter

# Verify startup probe passed (look for "startup probe OK" in each unit's log):
journalctl -u iic-triage  -n 20 --no-pager | grep -E 'probe|resolved|fallback'
journalctl -u iic-promoter -n 20 --no-pager | grep -E 'probe|resolved|fallback'
```

The daemons log the resolved provider/model/base_url/fallback at startup.
With `fallback: none` (the compiled default), a dead probe refuses to start
— this is intentional: degrade loudly, not silently.

---

## 3. Fallback flip (`fallback: none` → `fallback: api`)

**Important:** `fallback` is a config-file / code-default value.
**There is no env var for it.** The `_ENV_OVERRIDES` table in
`tradingagents/default_config.py` does not include a `fallback` entry, and
`_apply_nested_env_overrides` does not map any env var to
`llm_roles.<role>.fallback`.

To flip a role from `fallback: "none"` to `fallback: "api"`, you must either:

1. **Edit `tradingagents/default_config.py`** — change the `"fallback": "none"`
   entry in the `triage_salience` or `alert_gate` block to `"fallback": "api"`.
   This is the committed default; a code change + redeploy is required.
2. **Add an env-var override as future work** — a `IIC_TRIAGE_LLM_FALLBACK`
   / `IIC_ALERT_GATE_LLM_FALLBACK` entry could be added to
   `_apply_nested_env_overrides` following the same pattern as the existing
   `_role_env_map` entries; that task is not yet done.

When `fallback: "api"` is active, a dead startup probe or a consecutive
runtime failure run that reaches `fallback_threshold` (default: 3) causes
the role to re-resolve to the global provider. The daily fallback budget
(`fallback_daily_budget: 500` calls/UTC-day) caps the API spend. Budget
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
# 1. Stop IIC daemons that use the local endpoint
sudo systemctl stop iic-triage iic-promoter

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
| Shadow soak | 24 h (run `--soak-report` after the window) |

```bash
# After the 24h shadow soak:
python scripts/f4_f5_exit_gate.py --soak-report --local-model-id <new-model-id>
# Add --json for machine-readable output
```

Once both gates pass, restart the IIC daemons:

```bash
sudo systemctl daemon-reload
sudo systemctl start iic-triage iic-promoter
```

---

## 5. Revert (unset the two provider vars)

The revert is a pure env unset — no code changes required.

Remove (or comment out) the two provider vars from `.env`:

```bash
# Remove or comment out:
# IIC_TRIAGE_LLM_PROVIDER=local
# IIC_ALERT_GATE_LLM_PROVIDER=local
```

`LOCAL_LLM_BASE_URL`, `LOCAL_LLM_API_KEY`, `IIC_TRIAGE_LLM_MODEL`, and
`IIC_ALERT_GATE_LLM_MODEL` are **safe to leave set** — a model entry without
a provider override is inert and the role falls back to the global
`llm_provider` default (currently `deepseek`).

Then reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart iic-triage iic-promoter

# Confirm roles resolved to the API provider:
journalctl -u iic-triage  -n 10 --no-pager | grep 'resolved:'
journalctl -u iic-promoter -n 10 --no-pager | grep 'resolved:'
```

---

## 6. Soak verification and observability

### Soak report

```bash
python scripts/f4_f5_exit_gate.py --soak-report [--local-model-id <id>] [--json]
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
sqlite3 /home/ziwei-huang/.tradingagents/iic.db \
    "SELECT name, value FROM ops_counters WHERE name LIKE '%llm%' ORDER BY name"
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
- **CRITICAL log**: always emitted regardless of Telegram config; visible via
  `journalctl -u iic-triage -p crit` / `journalctl -u iic-promoter -p crit`.

The alert is debounced: one alert per outage, not one per failure. A
recovered endpoint re-arms the latch so the next outage alerts again.
