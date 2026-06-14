# IIC-Forge — Early-Testing Local-LLM Fallback

**Date:** 2026-06-13
**Status:** Design — approved, pending implementation plan
**Scope:** Make the existing local→cloud LLM fallback safe and turnkey to enable
for the early-testing phase, with a **dedicated, removable API key** that keeps
the classification fallback structurally isolated from the workers' persistent
cloud key — without weakening the fail-closed production default.

---

## 1. Problem

The system already has a local-LLM fallback (the D5 policy in
`tradingagents/llm_clients/availability.py`), wired into the two classification
roles — `triage_salience` (triage daemon) and `alert_gate` (promoter daemon).
It probes a local endpoint at startup and, when `fallback="api"`, re-resolves
the role to the global cloud provider (DeepSeek/OpenAI) both at startup (dead
probe) and at runtime (after `fallback_threshold` consecutive failures),
hard-bounded by a per-UTC-day call budget.

It is unfriendly to turn on for early testing, and — more importantly — it
**shares one API key with the persistent worker path**:

1. It ships **disabled**: compiled default `fallback="none"`, and
   `ops/env.iic-forge.example` leaves `IIC_LLM_FALLBACK_MODE` at `none`.
2. **The `budget=0` footgun.** `ops/env.iic-forge.example` sets
   `IIC_LLM_FALLBACK_DAILY_BUDGET=0`, which overrides the compiled default of
   `500`. With `fallback="api"` + budget `0`, `DailyFallbackBudget.try_consume()`
   returns `False` on every call, so **every** fallback call raises
   `LocalEndpointUnavailable`. The fallback looks enabled but can never fire,
   and nothing flags the contradiction at startup.
3. **Key entanglement.** The persistent worker studies resolve their LLM via
   `create_llm_client` (`tradingagents/graph/trading_graph.py:105-113`) and the
   classification fallback resolves via `create_role_llm` →
   `resolve_role_llm_global`. **Both end at `OpenAIClient.get_llm()`**, which
   for `deepseek` reads `DEEPSEEK_API_KEY` from the environment
   (`openai_client.py:218-222`). So today the temporary, testing-only
   classification fallback and the workers' always-on cloud usage draw on the
   **same key** — removing a "testing key" after deployment is impossible
   without also breaking the workers.
4. There is no documented, copy-paste recipe for the testing posture.

During early testing we want the opposite of production: when the local model
is down or not yet stood up, keep the classification pipeline running on a
**separate, throwaway cloud key** rather than refusing to start — while
bounding spend, keeping the change local to the operator's private `.env`, and
guaranteeing that key can never bleed into the workers' persistent path.

## 2. Goals

- An operator can enable local→cloud fallback for `triage_salience` and
  `alert_gate` by editing **only their private `.env`**.
- The classification fallback uses a **dedicated API key**
  (`IIC_LLM_FALLBACK_API_KEY`), structurally separate from the workers'
  persistent `DEEPSEEK_API_KEY`. Removing the dedicated key after deployment
  cleanly severs the classification fallback and leaves the workers untouched.
- When `fallback=api` is active but the dedicated key is **absent**, the
  classification fallback **refuses** (raises) rather than borrowing
  `DEEPSEEK_API_KEY` — isolation by construction, not by remembering to also
  flip `fallback=none`.
- The committed production template (`ops/env.iic-forge.example`) stays
  **fail-closed** (`fallback=none`, budget `0`, no fallback key).
- `fallback=api` that can never fire (budget ≤ 0, or dedicated key missing) is
  surfaced **loudly at startup**, not silently per-call at runtime.
- A documented, copy-paste testing recipe and a post-deployment teardown.

## 3. Non-goals (YAGNI)

- No flip of the committed env template defaults.
- No new committed testing env file.
- No stub/offline/deterministic classifier (a separate option, deferred).
- No secondary-local-model fallback chain.
- No change to the heavy `worker-deep` study path — it keeps `DEEPSEEK_API_KEY`
  and its existing resolution untouched.
- No per-role fallback keys — one shared `IIC_LLM_FALLBACK_API_KEY` covers both
  classification roles. (Per-role split is a trivial future extension.)
- No dedicated fallback **provider/model** override — the fallback keeps using
  the global provider/model, only the key is separated. (Future extension.)
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
    → raises `LocalEndpointUnavailable`; `fallback="api"` →
    `resolve_role_llm_global` (cloud), `used_fallback=True`.
  - `resolve_role_llm_global(role, config)` → strips the role override and
    rebuilds via `factory.create_role_llm` against the **global** provider —
    today inheriting `DEEPSEEK_API_KEY`.
  - `DailyFallbackBudget(name, max_per_day, conn)` → `try_consume()` returns
    `False` when the UTC-day budget is exhausted; persisted in `ops_counters`.
  - `AvailabilityCounter` drives runtime fallback engagement (consecutive
    failures ≥ `fallback_threshold`) and the one-shot self-alert.
- **Key plumbing** (`tradingagents/llm_clients/openai_client.py`):
  - `get_llm()` for a `_PROVIDER_BASE_URL` provider (incl. `deepseek`) reads the
    key from the provider-mapped env var (`get_api_key_env` →
    `DEEPSEEK_API_KEY`) at lines 218-222, and **raises** at line 229 if that env
    var is unset and the provider is not optional.
  - `api_key` is in `_PASSTHROUGH_KWARGS` (line 145); the passthrough loop
    (lines 240-242) runs *after* the env read, so an explicit `api_key` kwarg
    overrides the env-derived key — **but only when the provider env var is set**
    (otherwise line 229 raises first). This precedence is currently fragile.
- **Consumers** read `fallback_mode = role_cfg.get("fallback")` and budget
  `max_per_day = int(role_cfg.get("fallback_daily_budget", 500))` in:
  - `tradingagents/sensing/triage.py` `_main`
  - `tradingagents/orchestrator/promoter.py` `_main`

## 5. Design

### 5.1 Dedicated, isolated fallback key

New env var **`IIC_LLM_FALLBACK_API_KEY`** (fits the existing
`IIC_LLM_FALLBACK_*` family). It is the testing-only key for the classification
cloud fallback. It is **not** mapped into `llm_roles` config like the other
`IIC_LLM_FALLBACK_*` vars; it is consumed directly at fallback-resolution time
(see 5.2) so it can be injected as an explicit client key and validated for
presence.

### 5.2 Fallback resolver injects the dedicated key (structural isolation)

`resolve_role_llm_global` (the classification fallback resolver) changes so the
fallback client is built with `api_key = os.environ.get("IIC_LLM_FALLBACK_API_KEY")`:

- **Key present** → pass it as an explicit `api_key` to `create_role_llm`'s
  client so `get_llm()` uses it, never `DEEPSEEK_API_KEY`.
- **Key absent** → raise `LocalEndpointUnavailable` with a clear message
  (`"classification fallback enabled (fallback=api) but IIC_LLM_FALLBACK_API_KEY
  is not set"`). The fallback is **unavailable**; it never borrows the worker
  key. (Decided: refuse, not fall through.)

The worker path (`create_llm_client`) is unchanged and keeps reading
`DEEPSEEK_API_KEY`.

### 5.3 Client honors an explicit `api_key`

Small change in `OpenAIClient.get_llm()`: when an explicit `api_key` is present
in `self.kwargs`, use it as the highest-precedence key and **skip** the
env-var read and the missing-env-var raise (lines 218-233). This makes per-client
keys first-class and removes the fragile coupling noted in §4. The worker path
passes no explicit `api_key`, so its behaviour (read `DEEPSEEK_API_KEY`, raise if
unset) is unchanged.

### 5.4 Startup guardrail

Add a shared helper in `availability.py`:

```python
def warn_if_fallback_unsatisfiable(role, fallback_mode, max_per_day,
                                   *, fallback_key_present, log):
    """Loudly warn when fallback=api can never fire: budget<=0 OR the dedicated
    fallback key is missing. No-op for every other combination."""
```

Behaviour:

- Fires only when `fallback_mode == "api"` **and** (`max_per_day <= 0` **or**
  `not fallback_key_present`).
- Emits a single `log.warning(...)` per reason, naming the role and the exact
  remedy (set `IIC_LLM_FALLBACK_DAILY_BUDGET > 0` and/or set
  `IIC_LLM_FALLBACK_API_KEY`).
- All other combinations are silent no-ops, so the fail-closed production
  default produces no new noise.

Called from `triage._main` and `promoter._main` **before** the probe/resolution
step (`resolve_role_llm_with_fallback`), so the warning always fires at startup
regardless of local-endpoint health. Its inputs — `fallback_mode`, the budget,
and `IIC_LLM_FALLBACK_API_KEY` presence — are all available from config/env
before resolution. (The implementation plan may need to hoist the
`fallback_mode`/budget reads slightly earlier than their current position.)

**Warn, not hard-fail (decided).** The daemon still starts. When the local
endpoint is healthy the misconfig is harmless — fallback is a disabled safety
net — so blocking startup over a latent issue is too aggressive, and the
existing runtime path already raises loudly if the fallback is actually reached
unsatisfiable. (The *resolution-time* refuse in 5.2 only fires when the fallback
is actually engaged with no key.)

### 5.5 Testing recipe (docs only)

Add an "Early-testing fallback" subsection to `ops/runbooks/local-llm.md`
(near the existing §3) and a one-line pointer from
`ops/runbooks/operating-guide.md` §4.7. The recipe:

```dotenv
# Private .env only — NEVER edit ops/env.iic-forge.example
IIC_LLM_FALLBACK_MODE=api            # both classification roles → cloud on local outage
IIC_LLM_FALLBACK_DAILY_BUDGET=500    # hard per-UTC-day cap (compiled default; tune to taste)
IIC_LLM_FALLBACK_API_KEY=<throwaway-testing-key>   # SEPARATE from DEEPSEEK_API_KEY
# DEEPSEEK_API_KEY stays the workers' persistent key — do not reuse it above.
```

Verify:

```bash
docker compose restart triage promoter
docker compose logs --tail=20 triage   | grep -E 'resolved:|fallback'
docker compose logs --tail=20 promoter | grep -E 'resolved:|fallback'
```

**Post-deployment teardown:** remove `IIC_LLM_FALLBACK_API_KEY` *and* set
`IIC_LLM_FALLBACK_MODE` back to `none` (or drop it), then restart triage/promoter.
Either lock alone severs the classification fallback; `DEEPSEEK_API_KEY` (workers)
is untouched. The committed `ops/env.iic-forge.example` never changes.

### 5.6 Tests

- **Key isolation** (`tests/llm_clients/`): with `DEEPSEEK_API_KEY=worker-key`
  and `IIC_LLM_FALLBACK_API_KEY=test-key`, the worker client resolves to
  `worker-key` and the classification fallback client resolves to `test-key`
  (assert the two differ and the fallback never carries `worker-key`).
- **Refuse on missing key**: `fallback=api` engaged with
  `IIC_LLM_FALLBACK_API_KEY` unset → `resolve_role_llm_global` raises
  `LocalEndpointUnavailable`; it does **not** build a client carrying
  `DEEPSEEK_API_KEY`.
- **Explicit-key precedence**: `OpenAIClient.get_llm()` with an explicit
  `api_key` uses it even when the provider env var is unset (no raise).
- **Guardrail**: warning emitted for `api`+budget 0 and for `api`+missing key;
  not emitted for `none`+0, `none`+500, or `api`+budget>0+key-present.
- **Fallback-engagement regression**: probe fails + `fallback="api"` + key
  present → routes through `resolve_role_llm_global`, `used_fallback=True`
  (using the injectable `probe=` seam).

## 6. Data flow

No new tables. The dedicated key is read from the environment at
fallback-resolution time; nothing is persisted for it. The budget counter
continues to live in `ops_counters`. The env-template contract test is
unaffected (the template is not edited). The worker resolution path is byte-for-
byte unchanged.

## 7. Acceptance criteria

1. With a private `.env` setting `IIC_LLM_FALLBACK_MODE=api`,
   `IIC_LLM_FALLBACK_DAILY_BUDGET=500`, and `IIC_LLM_FALLBACK_API_KEY=<test>`,
   a dead local endpoint lets triage and promoter **start** and run the
   classification fallback on the **test key** (not `DEEPSEEK_API_KEY`).
2. Worker studies continue to use `DEEPSEEK_API_KEY`; removing
   `IIC_LLM_FALLBACK_API_KEY` does not affect the worker path.
3. With `fallback=api` but `IIC_LLM_FALLBACK_API_KEY` unset, an engaged fallback
   raises `LocalEndpointUnavailable` and never carries `DEEPSEEK_API_KEY`; both
   daemons log the guardrail warning once at startup.
4. With `fallback=api` but budget `0`, both daemons start and log the guardrail
   warning once each at startup.
5. The committed `ops/env.iic-forge.example` is unchanged; its contract test
   still passes; the production default (`none`, budget 0, no key) produces no
   new warning.
6. New unit tests pass; `python -m pytest tests/llm_clients -q` is green.

## 8. Files touched

- `tradingagents/llm_clients/openai_client.py` — explicit `api_key` precedence
  in `get_llm()` (skip env read + raise when provided).
- `tradingagents/llm_clients/availability.py` — inject
  `IIC_LLM_FALLBACK_API_KEY` in `resolve_role_llm_global`, refuse when absent;
  add `warn_if_fallback_unsatisfiable`.
- `tradingagents/sensing/triage.py` — call the guardrail in `_main`.
- `tradingagents/orchestrator/promoter.py` — call the guardrail in `_main`.
- `ops/runbooks/local-llm.md` — early-testing recipe + teardown.
- `ops/runbooks/operating-guide.md` — pointer to the recipe.
- `tests/llm_clients/...` — key-isolation, refuse-on-missing, explicit-key
  precedence, guardrail, and engagement tests.

No change to `ops/env.iic-forge.example`, `compose.yml`, the
`default_config.py` defaults, or the worker LLM resolution path.
