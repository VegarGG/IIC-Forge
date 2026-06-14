# IIC-Forge — Early-Testing Local-LLM Fallback

**Date:** 2026-06-13
**Status:** Design — approved, pending implementation plan
**Scope:** Make the existing local→cloud LLM fallback safe and turnkey to enable
for the early-testing phase, without weakening the fail-closed production
default.

---

## 1. Problem

The system already has a local-LLM fallback (the D5 policy in
`tradingagents/llm_clients/availability.py`), wired into the two classification
roles — `triage_salience` (triage daemon) and `alert_gate` (promoter daemon).
It probes a local endpoint at startup and, when `fallback="api"`, re-resolves
the role to the global cloud provider (DeepSeek/OpenAI) both at startup (dead
probe) and at runtime (after `fallback_threshold` consecutive failures),
hard-bounded by a per-UTC-day call budget.

It is, however, unfriendly to turn on for early testing:

1. It ships **disabled**: compiled default `fallback="none"`, and
   `ops/env.iic-forge.example` sets `IIC_LLM_FALLBACK_MODE` unset / `none`.
2. **The `budget=0` footgun.** `ops/env.iic-forge.example` sets
   `IIC_LLM_FALLBACK_DAILY_BUDGET=0`, which overrides the compiled default of
   `500`. With `fallback="api"` + budget `0`, `DailyFallbackBudget.try_consume()`
   returns `False` on every call, so **every** fallback call raises
   `LocalEndpointUnavailable`. The fallback appears enabled but can never
   actually fire — and nothing flags the contradiction at startup.
3. There is no documented, copy-paste recipe for the testing posture.

During early testing we want the opposite of production: when the local model
is down or not yet stood up, keep the pipeline running on the cloud API rather
than refusing to start — while still bounding spend and keeping the change
local to the operator's private `.env`.

## 2. Goals

- An operator can enable local→cloud fallback for `triage_salience` and
  `alert_gate` by editing **only their private `.env`**.
- The committed production template (`ops/env.iic-forge.example`) stays
  **fail-closed** (`fallback=none`, budget `0`) — no risk of shipping a
  fail-open posture to production.
- The `fallback=api` + `budget<=0` misconfiguration is surfaced **loudly at
  startup** instead of failing silently per-call at runtime.
- A documented, copy-paste testing recipe and revert.

## 3. Non-goals (YAGNI)

- No flip of the committed env template defaults.
- No new committed testing env file.
- No stub/offline/deterministic classifier (a separate option, deferred).
- No secondary-local-model fallback chain.
- No fallback for the heavy `worker-deep` study path (it already runs on the
  cloud API directly; the local model is not in that path).
- No new "environment mode" switch (`IIC_ENV=testing|production`).

## 4. Current mechanism (ground truth)

Reference, so the implementation plan targets the right seams.

- **Env → config mapping** (`tradingagents/default_config.py`
  `_apply_nested_env_overrides`):
  - `IIC_LLM_FALLBACK_MODE` → `llm_roles[role]["fallback"]` for both roles.
  - `IIC_TRIAGE_LLM_FALLBACK_MODE` / `IIC_ALERT_GATE_LLM_FALLBACK_MODE` →
    per-role `fallback` (precedence over global).
  - `IIC_LLM_FALLBACK_DAILY_BUDGET` → `llm_roles[role]["fallback_daily_budget"]`
    (float) for both roles (global only — no per-role budget variant).
  - Compiled per-role defaults: `fallback="none"`, `fallback_threshold=3`,
    `fallback_daily_budget=500`.
- **Resolution** (`availability.py`):
  - `resolve_role_llm_with_fallback(role, config, *, probe=None)` →
    `(client, used_fallback)`. Local provider + dead probe + `fallback="none"`
    → raises `LocalEndpointUnavailable` (refuse to start); `fallback="api"` →
    `resolve_role_llm_global` (cloud), `used_fallback=True`.
  - `DailyFallbackBudget(name, max_per_day, conn)` →
    `try_consume()` returns `False` when the UTC-day budget is exhausted;
    counter persisted in `ops_counters`.
  - `AvailabilityCounter` drives runtime fallback engagement (consecutive
    failures ≥ `fallback_threshold`) and the one-shot self-alert.
- **Consumers** read `fallback_mode = role_cfg.get("fallback")` and budget
  `max_per_day = int(role_cfg.get("fallback_daily_budget", 500))` in:
  - `tradingagents/sensing/triage.py` `_main`
  - `tradingagents/orchestrator/promoter.py` `_main`

## 5. Design

### 5.1 Guardrail (the only code change)

Add a shared helper in `tradingagents/llm_clients/availability.py`:

```python
def warn_if_fallback_unsatisfiable(role, fallback_mode, max_per_day, *, log):
    """Loudly warn when fallback=api can never fire because the daily budget
    is non-positive. No-op for every other combination."""
```

Behaviour:

- Fires **only** when `fallback_mode == "api"` **and** `max_per_day <= 0`.
- Emits a single `log.warning(...)` naming the role and the exact remedy:
  *"role <role> has fallback=api but daily budget <max>: fallback will NEVER
  fire (every fallback call will raise LocalEndpointUnavailable). Set
  IIC_LLM_FALLBACK_DAILY_BUDGET > 0 to make fallback effective."*
- All other combinations (`none`+0, `none`+500, `api`+500) are silent no-ops,
  so the fail-closed production default produces no new noise.

Call it from `triage._main` and `promoter._main` immediately after each computes
`fallback_mode` and the budget `max_per_day`, before entering the work loop.

**Warn, not hard-fail (decided).** The daemon still starts. When the local
endpoint is healthy the misconfig is harmless — fallback is simply a disabled
safety net — so blocking startup over a latent issue is too aggressive, and the
existing runtime path already raises `LocalEndpointUnavailable` loudly if the
fallback is actually reached with no budget.

### 5.2 Testing recipe (docs only)

Add an "Early-testing fallback" subsection to `ops/runbooks/local-llm.md`
(near the existing §3 fallback content) and a one-line pointer from
`ops/runbooks/operating-guide.md` §4.7. The recipe:

```dotenv
# Private .env only — NEVER edit ops/env.iic-forge.example
IIC_LLM_FALLBACK_MODE=api            # both classification roles → cloud on local outage
IIC_LLM_FALLBACK_DAILY_BUDGET=500    # hard per-UTC-day cap (compiled default; tune down to taste)
DEEPSEEK_API_KEY=...                 # the fallback target must be configured
```

Then:

```bash
docker compose restart triage promoter
docker compose logs --tail=20 triage   | grep -E 'resolved:|fallback'
docker compose logs --tail=20 promoter | grep -E 'resolved:|fallback'
```

Revert = remove the two `IIC_LLM_FALLBACK_*` lines and restart triage/promoter.
The committed `ops/env.iic-forge.example` is unchanged (`mode` unset/`none`,
budget `0`).

Budget guidance: 500/day (the compiled default) is ample for testing and bounds
spend — classification calls are short `quick_think_llm` prompts. Lower it
freely.

### 5.3 Tests

- **Guardrail unit test** (`tests/llm_clients/`): asserts the warning is emitted
  for `api`+`0` (and `api`+negative), and **not** emitted for `none`+`0`,
  `none`+`500`, or `api`+`500`. Capture via `caplog`.
- **Fallback-engagement regression** (extend existing
  `availability` / triage tests if present, else add): probe fails +
  `fallback="api"` routes through `resolve_role_llm_global` and returns
  `used_fallback=True`, using the injectable `probe=` seam in
  `resolve_role_llm_with_fallback`. This guards that the documented testing
  path actually engages.

## 6. Data flow (unchanged)

No new tables, env contracts, or call paths. The guardrail is a read-only
startup observation; the budget counter continues to live in `ops_counters`.
The env template contract test (which asserts `ops/env.iic-forge.example`
contents) is unaffected because the template is not edited.

## 7. Acceptance criteria

1. With a private `.env` setting `IIC_LLM_FALLBACK_MODE=api` +
   `IIC_LLM_FALLBACK_DAILY_BUDGET=500` + `DEEPSEEK_API_KEY`, a dead local
   endpoint lets triage and promoter **start** and run on the cloud provider
   (no refuse-to-start).
2. With `IIC_LLM_FALLBACK_MODE=api` + budget `0`, both daemons start and log the
   guardrail warning exactly once each at startup.
3. The committed `ops/env.iic-forge.example` is unchanged; its contract test
   still passes.
4. Production default (`none` + 0) produces no new warning.
5. New unit tests pass; `python -m pytest tests/llm_clients -q` is green.

## 8. Files touched

- `tradingagents/llm_clients/availability.py` — add `warn_if_fallback_unsatisfiable`.
- `tradingagents/sensing/triage.py` — call the guardrail in `_main`.
- `tradingagents/orchestrator/promoter.py` — call the guardrail in `_main`.
- `ops/runbooks/local-llm.md` — add the early-testing recipe.
- `ops/runbooks/operating-guide.md` — pointer to the recipe.
- `tests/llm_clients/...` — guardrail + engagement tests.

No change to `ops/env.iic-forge.example`, `compose.yml`, or
`default_config.py` defaults.
