# IIC-FORGE_05 Local Model Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the two always-on, high-frequency classification workloads — **triage salience scoring** and the **promoter alert gate** (strict evaluator + light-alert summary) — to a config-selectable **local quick model** served by `llama-server` over LAN, while leaving every other LLM path (graph analysts, debates, trader, PM, synthesis, deep-dive, refinement, morning digest) on the API providers unchanged. The swap lands entirely behind config: roles default to the global API provider until two env vars are flipped, so trunk stays releasable at every step and cutover/revert is a pure config operation.

**Architecture:** A new first-class `local` provider rides the existing `_OPENAI_COMPATIBLE` path (llama-server speaks the OpenAI chat-completions API). A per-role routing layer (`llm_roles` config + a `create_role_llm(role, config)` factory helper) resolves *role → override → global fallback*, so only the triage `_main` and promoter `main` call sites change (two lines each). Classification parsing is hardened with schema-constrained decoding (json_schema / GBNF grammar) and parse/latency telemetry. A `scripts/shadow_eval.py` harness replays stored events through both the API quick model and the local endpoint to pick between candidate weights (**Qwen 3.6 27B** vs **DeepSeek V4 Flash** GGUF) on measured agreement before any cutover. Availability is a new failure domain handled by an eager startup probe, loud per-cycle degradation (deferred scores, skipped cycles, failure counters), and an opt-in deliberate API fallback.

**Tech Stack:** Python 3.10+, SQLite (`tradingagents.persistence`, WAL, `sqlite-vec`), LangChain `ChatOpenAI` via `OpenAIClient`, llama.cpp `llama-server` (OpenAI-compatible), pytest (`@pytest.mark.unit`), Redis (ingest/dedupe only). Repo root for all paths: `/home/user/IIC-Forge`. Run tests with `python -m pytest`.

**Spec:** `docs/superpowers/specs/IIC-FORGE_05_Local_Model_Reconstruction.md`

**Baseline assumption:** FORGE_04 Phase A (promoter terminal-state fix, delivery merge, Telegram repair) lands **first** — this reconstruction does not absorb or excuse those fixes. L0–L1 may proceed in parallel with FORGE_04 Phase B; L2 onward needs the test box reachable on LAN.

**Conventions observed:**
- TDD per task: write the failing test, run it red, implement, run it green, commit. One commit per green task.
- Run a single test: `python -m pytest <path>::<test> -v`. Run a subsystem: `python -m pytest tests/llm_clients -q`.
- Branch is `claude/iic-forge-05-plan-hccyvy` (already checked out). Do **not** open a PR; push to the fork only when explicitly asked.
- Tests build a fresh DB with `connect(str(tmp_path / "iic.db"))`; `connect` auto-creates the schema and is idempotent (`CREATE … IF NOT EXISTS`; `ALTER TABLE ADD COLUMN` re-runs are tolerated by `db.connect`'s "duplicate column name" suppression — new columns are added the same way).
- Roles **default to the global provider** until explicitly overridden. Merging L0 is a no-op on the production box until the env flips.
- No CI GPU: the local endpoint is exercised in unit/contract tests against a stub OpenAI-compatible server (FastAPI fixture); real-endpoint behavior is gated by the L2 shadow harness and soaks.

---

## File Structure

**Modify:**
- `tradingagents/llm_clients/factory.py` — add `"local"` to `_OPENAI_COMPATIBLE`; add `create_role_llm(role, config)` helper.
- `tradingagents/llm_clients/openai_client.py` — `local` base URL default `http://127.0.0.1:8080/v1` + `LOCAL_LLM_BASE_URL` override in `_resolve_provider_base_url`; optional-key handling in `get_llm`; `extra_body` / `chat_template_kwargs` passthrough.
- `tradingagents/llm_clients/api_key_env.py` — map `"local"` → `LOCAL_LLM_API_KEY`; add `OPTIONAL_KEY_PROVIDERS = {"local", "ollama"}` and `is_optional_key(provider)`.
- `tradingagents/llm_clients/capabilities.py` — capability rows for the two candidate local model IDs (`supports_json_schema=True`, `preferred_structured_method="json_schema"`, no reasoning round-trip).
- `tradingagents/llm_clients/model_catalog.py` — catalog rows for the candidate local GGUF model IDs.
- `tradingagents/default_config.py` — `llm_roles` block + env mapping (`IIC_TRIAGE_LLM_PROVIDER/MODEL`, `IIC_ALERT_GATE_LLM_PROVIDER/MODEL`, `LOCAL_LLM_BASE_URL`); delete dead `refinement.classifier_llm`.
- `tradingagents/sensing/triage.py` — `_main` builds the quick client via `create_role_llm("triage_salience", C)`; wrap LLM/embed calls in `asyncio.to_thread`.
- `tradingagents/sensing/salience.py` — schema-constrained parse via Pydantic model; remove the fallback-result caching (lines ~109–117); `salience_source='deferred'` on local-endpoint failure under `fallback: none`.
- `tradingagents/sensing/prompts.py` — (no rewrite) confirm cache-stable prefix tests still bind; add a salience JSON-schema export if the prompt references it.
- `tradingagents/orchestrator/promoter.py` — `main` builds the gate client via `create_role_llm("alert_gate", cfg)`; eager startup probe; per-cycle skip + failure counter on endpoint failure.
- `tradingagents/orchestrator/alert_evaluator.py` — `json_schema` response format from `AlertEvaluationPayload`; record `model_id`, `parse_ok`, `latency_ms`.
- `tradingagents/persistence/schema.sql` — `ALTER TABLE alert_evaluations ADD COLUMN model_id / parse_ok / latency_ms`; new `shadow_eval` table.
- `tradingagents/persistence/store.py` — insert/query helpers for the new evaluator columns and `shadow_eval`.
- `tradingagents/dashboard/panels/costs.py` — local-vs-API call-volume split; treat `usd_estimate=0.0` as *free* (distinct from `NULL` = unknown).
- `ops/systemd/iic-triage.service`, `ops/systemd/iic-promoter.service` — `LOCAL_LLM_BASE_URL` (and optional `LOCAL_LLM_API_KEY`) env for these two units only.

**Create:**
- `tests/llm_clients/__init__.py`, `tests/llm_clients/conftest.py` — FastAPI stub OpenAI-compatible server fixture (captures request bodies; returns canned chat-completion JSON).
- `tests/llm_clients/test_local_provider.py` — base URL, optional key, capability resolution.
- `tests/llm_clients/test_create_role_llm.py` — role → override → global fallback; `extra_body`/`enable_thinking=false` resolution.
- `tests/llm_clients/test_local_contract.py` — request-shape contract (json_schema, `chat_template_kwargs.enable_thinking=false`) against the stub server.
- `tests/sensing/test_salience_schema_parse.py` — schema-constrained parse, no fallback caching, `deferred` on failure.
- `tests/sensing/test_triage_loop_nonblocking.py` — loop does not block on a slow LLM call.
- `tests/orchestrator/test_alert_evaluator_telemetry.py` — `model_id`/`parse_ok`/`latency_ms` recorded.
- `tests/orchestrator/test_promoter_local_availability.py` — eager probe refuses start on failure (fallback none); per-cycle skip + counter at runtime.
- `tests/scripts/test_shadow_eval.py` — replay harness + gate-report metrics (MAE, threshold-crossing agreement, κ, latency, parse rate) on synthetic rows.
- `scripts/shadow_eval.py` — replay last N events/candidates through API + local; write `shadow_eval` rows; print the acceptance report.
- `ops/runbooks/local-llm.md` — probe commands, fallback flip, model-swap procedure (replace GGUF + restart llama-server, no IIC change).

---

## Phase L0 — Plumbing (no behavior change; roles default to global API)

### Task 1: First-class `local` provider (D1)

**Files:**
- Modify: `tradingagents/llm_clients/factory.py`, `tradingagents/llm_clients/openai_client.py`, `tradingagents/llm_clients/api_key_env.py`
- Create: `tests/llm_clients/__init__.py`, `tests/llm_clients/test_local_provider.py`

- [ ] **Step 1: Write the failing test**

Create `tests/llm_clients/test_local_provider.py`:

```python
import pytest
from tradingagents.llm_clients.factory import create_llm_client, _OPENAI_COMPATIBLE
from tradingagents.llm_clients.openai_client import _resolve_provider_base_url


@pytest.mark.unit
def test_local_is_openai_compatible():
    assert "local" in _OPENAI_COMPATIBLE


@pytest.mark.unit
def test_local_default_base_url():
    assert _resolve_provider_base_url("local") == "http://127.0.0.1:8080/v1"


@pytest.mark.unit
def test_local_base_url_env_override(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://192.168.1.50:8080/v1")
    assert _resolve_provider_base_url("local") == "http://192.168.1.50:8080/v1"


@pytest.mark.unit
def test_local_client_builds_without_api_key(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    client = create_llm_client(provider="local", model="qwen3.6-27b-instruct-q4_k_m")
    # Building the langchain object must NOT raise on a missing key.
    client.get_llm()
```

- [ ] **Step 2: Run red** — `python -m pytest tests/llm_clients/test_local_provider.py -v` → FAIL.

- [ ] **Step 3: Implement.** In `factory.py` add `"local"` to `_OPENAI_COMPATIBLE`. In `openai_client.py`: add `"local": "http://127.0.0.1:8080/v1"` to `_PROVIDER_BASE_URL`, and in `_resolve_provider_base_url` add a `local`→`LOCAL_LLM_BASE_URL` env override branch (mirroring the existing `ollama`→`OLLAMA_BASE_URL` branch). In `api_key_env.py` map `"local": "LOCAL_LLM_API_KEY"`.

- [ ] **Step 4: Run green** — same command → PASS. (The no-key build is finished in Task 2; if `get_llm()` still raises here, that assertion stays red until Task 2 — split the commit accordingly or land Tasks 1+2 together.)

- [ ] **Step 5: Commit** — `git commit -m "feat(llm): first-class local provider (llama-server, OpenAI-compatible)"`

---

### Task 2: Optional-key provider semantics (D1)

Today `OpenAIClient.get_llm` raises when a mapped key env var is unset. `local` (and `ollama`) must treat the key as optional: `llama-server --api-key` is recommended on a LAN port, but absence must not raise.

**Files:**
- Modify: `tradingagents/llm_clients/api_key_env.py`, `tradingagents/llm_clients/openai_client.py`
- Test: extend `tests/llm_clients/test_local_provider.py`

- [ ] **Step 1: Write the failing test** — add to the test file:

```python
from tradingagents.llm_clients.api_key_env import OPTIONAL_KEY_PROVIDERS, is_optional_key

@pytest.mark.unit
def test_optional_key_providers():
    assert is_optional_key("local") and is_optional_key("ollama")
    assert not is_optional_key("deepseek")

@pytest.mark.unit
def test_local_uses_api_key_when_present(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-lan-secret")
    llm = create_llm_client(provider="local", model="qwen3.6-27b-instruct-q4_k_m").get_llm()
    assert llm.openai_api_key.get_secret_value() == "sk-lan-secret"
```

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** In `api_key_env.py` add `OPTIONAL_KEY_PROVIDERS = {"local", "ollama"}` and `def is_optional_key(provider) -> bool`. In `openai_client.get_llm`, change the key branch: when `api_key_env` is set but the env var is **absent**, only raise if `not is_optional_key(self.provider)`; for optional-key providers fall through to the placeholder key (`"ollama"`-style sentinel) instead of raising. When the env var **is** present, use it.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(llm): optional-key providers (local, ollama) build without a key"`

---

### Task 3: Capability + catalog rows for candidate local models (D3)

**Files:**
- Modify: `tradingagents/llm_clients/capabilities.py`, `tradingagents/llm_clients/model_catalog.py`
- Test: `tests/llm_clients/test_local_provider.py` (extend)

- [ ] **Step 1: Write the failing test:**

```python
from tradingagents.llm_clients.capabilities import get_capabilities

@pytest.mark.unit
@pytest.mark.parametrize("model", [
    "qwen3.6-27b-instruct-q4_k_m",
    "deepseek-v4-flash-gguf-q4_k_m",
])
def test_local_model_caps(model):
    caps = get_capabilities(model)
    assert caps.supports_json_schema is True
    assert caps.preferred_structured_method == "json_schema"
    assert caps.requires_reasoning_content_roundtrip is False
```

- [ ] **Step 2: Run red** → FAIL (default caps don't set `json_schema`).

- [ ] **Step 3: Implement.** In `capabilities.py` add a `_LOCAL_CLASSIFIER = ModelCapabilities(supports_tool_choice=False, supports_json_mode=True, supports_json_schema=True, preferred_structured_method="json_schema")` and register the two candidate IDs in `_BY_ID` (exact) plus a forward-compat pattern in `_BY_PATTERN` if the GGUF suffix varies (`^qwen3\.6-27b`, `^deepseek-v4-flash`). In `model_catalog.py` add a `_LOCAL_MODELS` quick-list block with the candidate GGUF IDs. Note: these IDs are **config values** — both candidates must be switchable without code, so the catalog rows are descriptive, not gating.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(llm): capability+catalog rows for local classifier models"`

---

### Task 4: `llm_roles` config block + env mapping; drop dead `classifier_llm` (D2)

**Files:**
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_default_config_llm_roles.py` (create)

- [ ] **Step 1: Write the failing test:**

```python
import pytest
from tradingagents.default_config import DEFAULT_CONFIG as C

@pytest.mark.unit
def test_llm_roles_default_to_global():
    roles = C["llm_roles"]
    for role in ("triage_salience", "alert_gate"):
        assert role in roles
        # Default ships with provider/model None so the role falls back to global.
        assert roles[role]["provider"] is None
        assert roles[role]["model"] is None
        assert roles[role]["fallback"] in ("none", "api")

@pytest.mark.unit
def test_classifier_llm_key_removed():
    assert "classifier_llm" not in C["refinement"]
```

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** Add to `default_config.py`:

```python
    # Per-role LLM routing. Each entry resolves role -> override -> global default
    # via create_role_llm(). Defaults are all-None so production behavior is
    # unchanged until the env vars below are set (shadow/cutover = config-only).
    "llm_roles": {
        "triage_salience": {"provider": None, "model": None, "base_url": None,
                            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                            "fallback": "none"},
        "alert_gate":      {"provider": None, "model": None, "base_url": None,
                            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                            "fallback": "none"},
    },
```

Add env mappings to the env→config table near the top (`IIC_TRIAGE_LLM_PROVIDER`→`llm_roles.triage_salience.provider`, `…_MODEL`→`.model`, same for `IIC_ALERT_GATE_LLM_*`, and `LOCAL_LLM_BASE_URL`). If the env loader is flat-key only, add a small post-load merge that maps these env vars into the nested `llm_roles` dict. **Delete** the `"classifier_llm": "quick_think_llm",` line from the `refinement` block (FORGE_04 dead key — subsumed by role routing).

- [ ] **Step 4: Run green** → PASS. Also run `python -m pytest tests/ -k refinement -q` to confirm nothing reads the deleted key.

- [ ] **Step 5: Commit** — `git commit -m "feat(config): llm_roles per-role routing block; drop dead classifier_llm"`

---

### Task 5: `create_role_llm(role, config)` factory helper (D2/D3)

Resolves a role to a built client: role override → global fallback; threads `extra_body` (thinking-off) through to the client.

**Files:**
- Modify: `tradingagents/llm_clients/factory.py`, `tradingagents/llm_clients/openai_client.py`
- Create: `tests/llm_clients/test_create_role_llm.py`

- [ ] **Step 1: Write the failing test:**

```python
import pytest
from tradingagents.llm_clients.factory import create_role_llm

BASE = {"llm_provider": "deepseek", "quick_think_llm": "deepseek-v4-flash",
        "backend_url": None, "llm_roles": {}}

@pytest.mark.unit
def test_role_falls_back_to_global_when_unset():
    cfg = {**BASE, "llm_roles": {"triage_salience": {"provider": None, "model": None,
                                                    "base_url": None, "extra_body": {}}}}
    client = create_role_llm("triage_salience", cfg)
    assert client.provider == "deepseek"
    assert client.model == "deepseek-v4-flash"

@pytest.mark.unit
def test_role_override_wins(monkeypatch):
    cfg = {**BASE, "llm_roles": {"alert_gate": {
        "provider": "local", "model": "qwen3.6-27b-instruct-q4_k_m",
        "base_url": None,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}}}
    client = create_role_llm("alert_gate", cfg)
    assert client.provider == "local"
    assert client.model == "qwen3.6-27b-instruct-q4_k_m"
    llm = client.get_llm()
    # extra_body must reach the langchain model so thinking is disabled.
    assert llm.extra_body["chat_template_kwargs"]["enable_thinking"] is False
```

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** Add `create_role_llm(role, config)` to `factory.py`:
  - read `config["llm_roles"][role]`; `provider = override.provider or config["llm_provider"]`; `model = override.model or config["quick_think_llm"]`; `base_url = override.base_url or config.get("backend_url")`.
  - pass `extra_body=override.get("extra_body")` into `create_llm_client(...)`. In `OpenAIClient`, accept `extra_body` and forward it to `ChatOpenAI` (add `"extra_body"` to `_PASSTHROUGH_KWARGS`, or set `llm_kwargs["extra_body"]` directly). `ChatOpenAI` forwards `extra_body` into the request body, where llama-server reads `chat_template_kwargs`.
  - missing role key → raise a clear `KeyError`-style error naming the role (config bug, fail loud).

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(llm): create_role_llm resolves role->override->global with extra_body"`

---

### Task 6: Stub-server contract test — request shape (L0)

No GPU in CI. Stand up a FastAPI fixture that accepts `POST /v1/chat/completions`, records the body, and returns a canned completion. Assert the local path sends `enable_thinking=false` and a json_schema response format.

**Files:**
- Create: `tests/llm_clients/conftest.py`, `tests/llm_clients/test_local_contract.py`

- [ ] **Step 1: Write the fixture + failing test.** `conftest.py` runs a uvicorn/`TestClient`-style app (or a threaded `http.server`) exposing `/v1/chat/completions` and `/health`, capturing the last request JSON. `test_local_contract.py`:

```python
import pytest

@pytest.mark.unit
def test_local_request_disables_thinking_and_uses_json_schema(stub_openai_server):
    cfg = {"llm_provider": "deepseek", "quick_think_llm": "deepseek-v4-flash",
           "backend_url": None,
           "llm_roles": {"triage_salience": {
               "provider": "local", "model": "qwen3.6-27b-instruct-q4_k_m",
               "base_url": stub_openai_server.url + "/v1",
               "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}}}
    from tradingagents.llm_clients.factory import create_role_llm
    llm = create_role_llm("triage_salience", cfg).get_llm()
    llm.invoke("classify this")  # round-trips through the stub
    body = stub_openai_server.last_request_json
    assert body["chat_template_kwargs"]["enable_thinking"] is False
```

- [ ] **Step 2: Run red** → FAIL (fixture/url plumbing).

- [ ] **Step 3: Implement** the fixture and any base_url plumbing needed so the role's `base_url` reaches the client.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "test(llm): stub-server contract for local request shape (no-think, json_schema)"`

---

### Task 7: Wire triage + promoter to `create_role_llm` (D2 — still a no-op)

Because the roles default to global, this changes routing **mechanism** without changing **behavior** until env is set.

**Files:**
- Modify: `tradingagents/sensing/triage.py` (`_main`, ~lines 384–393), `tradingagents/orchestrator/promoter.py` (`main`, ~lines 166–173)
- Test: `tests/sensing/test_triage_main_uses_role.py`, `tests/orchestrator/test_promoter_uses_role.py` (create; patch `create_role_llm` and assert it's called with the right role name)

- [ ] **Step 1: Write failing tests** that monkeypatch `tradingagents.llm_clients.factory.create_role_llm` and assert triage `_main` calls it with `"triage_salience"` and promoter `main` with `"alert_gate"` (stub Redis/DB/embedder so `_main`/`main` reach the call without doing real work; assert on the call args then bail early).

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** Triage `_main`: replace the `create_llm_client(provider=…, model=…, base_url=…)` block with `quick_client = create_role_llm("triage_salience", C)`. Promoter `main`: replace the `create_llm_client(...)` with `create_role_llm("alert_gate", cfg)`. Both keep `.get_llm()` and the existing wrapping (`call_llm`, the `Secretary(llm=…)` construction). The single promoter client still covers all three call sites (strict evaluator, candidate evaluator, light-alert summary) — verify the same `llm` object is threaded as before.

- [ ] **Step 2/4: Run green** → PASS. Run `python -m pytest tests/sensing tests/orchestrator -q` to confirm no regression.

- [ ] **Step 5: Commit** — `git commit -m "feat(routing): triage+promoter build their LLM via create_role_llm"`

---

### Task 8: Local-call cost telemetry rows (D6)

Local calls must record `provider='local'`, `usd_estimate=0.0` **explicitly** (not `NULL`, which means "unknown") so the dashboard distinguishes *free* from *unmetered*. The triage/promoter paths are not run-scoped (the `costs` table FKs `run_id`), so route their volume/latency into the new evaluator columns (Task 10) and a lightweight counter; apply the `provider/usd=0.0` rule wherever a role client *does* execute inside a run.

**Files:**
- Modify: `tradingagents/dashboard/panels/costs.py`
- Test: `tests/dashboard/test_costs_local_split.py` (create)

- [ ] **Step 1: Write the failing test** — seed `costs` rows with `provider='local', usd_estimate=0.0` and `provider='deepseek', usd_estimate=0.0012`; assert the panel reports a local-vs-API call split and treats `0.0` as *free* (counted) while `NULL` is *unknown* (excluded from the free tally).

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement** the split aggregation in `costs.py`; ensure `usd_estimate=0.0` is preserved as a real zero (not coalesced to NULL).

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(dashboard): costs panel splits local (free) vs API call volume"`

### ✅ Exit gate L0 (plumbing complete, zero behavior change)

- [ ] `local` provider builds with and without `LOCAL_LLM_API_KEY`; base URL honors `LOCAL_LLM_BASE_URL`.
- [ ] `create_role_llm` resolves role→override→global; roles ship all-None so production routing is byte-identical to today.
- [ ] Stub-server contract test green: local requests carry `enable_thinking=false` + json_schema; no GPU touched.
- [ ] Dead `refinement.classifier_llm` removed; nothing references it.
- [ ] `python -m pytest tests/llm_clients tests/sensing tests/orchestrator tests/dashboard -q` all green.
- [ ] Diff is a no-op on the prod box until env flips (manually confirm default config produces `provider='deepseek'` for both roles).

---

## Phase L1 — Classification hardening (still on API; ships value regardless of the box)

### Task 9: Schema-constrained salience parse + no fallback caching (D4)

**Files:**
- Modify: `tradingagents/sensing/salience.py`, `tradingagents/sensing/prompts.py` (schema export only)
- Test: `tests/sensing/test_salience_schema_parse.py` (create)

- [ ] **Step 1: Write the failing test:**

```python
import pytest
from tradingagents.sensing.salience import SalienceScorer, SalienceResult, SalienceSchema

@pytest.mark.unit
def test_salience_schema_matches_result_fields():
    # The Pydantic schema fed to json_schema response_format must cover the
    # fields _parse reads: salience, matched_tickers, mentioned_tickers, reason.
    fields = set(SalienceSchema.model_fields)
    assert {"salience", "matched_tickers", "mentioned_tickers", "reason"} <= fields

@pytest.mark.unit
async def test_failure_does_not_cache(fake_redis, monkeypatch):
    # LLM raises -> result is 'deferred', and NOTHING is written to redis.
    scorer = SalienceScorer(redis=fake_redis, llm_call=_raise, cache_ttl_seconds=86400)
    result = await scorer.score(env=_env(), watchlist=["NVDA"], macro_context="")
    assert result.source == "deferred"
    assert fake_redis.setex_calls == 0
```

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** Add a `SalienceSchema(BaseModel)` mirroring `SalienceResult`; expose it so the role client can request `response_format={"type":"json_schema", ...}` (the prompt builder / scorer passes it through). Change the `except` branch in `score` (lines ~109–117): **stop caching the failure** — return `SalienceResult(source="deferred", ...)` without `setex`. Only cache successful (`source="llm"`) results. Keep the fence-tolerant fallback parser for the **API** path; the local grammar path makes `invalid_json` structurally impossible.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(salience): json_schema parse; stop caching failures (deferred, not 0.1)"`

---

### Task 10: Evaluator schema + parse telemetry columns (D4)

**Files:**
- Modify: `tradingagents/orchestrator/alert_evaluator.py`, `tradingagents/persistence/schema.sql`, `tradingagents/persistence/store.py`
- Test: `tests/orchestrator/test_alert_evaluator_telemetry.py` (create)

- [ ] **Step 1: Write the failing test** — call `evaluate_alert_strict`/`evaluate_alert_candidate` against a fake LLM returning valid then malformed JSON; assert the `alert_evaluations` row records `model_id`, `parse_ok` (True/False), and a non-null `latency_ms`. Add a query helper test for `fetch_alert_eval_telemetry`.

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.**
  - `schema.sql`: `ALTER TABLE alert_evaluations ADD COLUMN model_id TEXT;` `… ADD COLUMN parse_ok INTEGER;` `… ADD COLUMN latency_ms INTEGER;` (idempotent re-run tolerated by `db.connect`).
  - `alert_evaluator.py`: derive `response_format` json_schema from `AlertEvaluationPayload`; time the call; on parse via `AlertEvaluationPayload.model_validate(json.loads(raw))`, set `parse_ok=True`, else `False` (and count it). Record `model_id` from the resolved client.
  - `store.py`: extend the `INSERT INTO alert_evaluations` to populate the new columns; add `fetch_alert_eval_telemetry`.
  - **Instrument the funnel:** these counters distinguish parse-failure rejects from genuine rejects (the FORGE_04 gap) and power the L2 agreement gate.

- [ ] **Step 4: Run green** → PASS. Run `python -m pytest tests/orchestrator/test_alert_evaluator.py -q` to confirm existing evaluator tests still pass.

- [ ] **Step 5: Commit** — `git commit -m "feat(evaluator): json_schema + parse/latency telemetry on alert_evaluations"`

---

### Task 11: Triage event loop is non-blocking (`to_thread`) (L1 / FORGE_04)

Local-endpoint latency variance makes a blocked event loop worse, and shadow mode (L2) doubles call volume. Wrap the sync LLM/embed calls so the consume loop never blocks.

**Files:**
- Modify: `tradingagents/sensing/triage.py`
- Test: `tests/sensing/test_triage_loop_nonblocking.py` (create)

- [ ] **Step 1: Write the failing test** — feed a `call_llm` that sleeps; assert the consume loop continues to drain other events concurrently (or assert the LLM call is dispatched via `asyncio.to_thread`, i.e. it does not run on the event-loop thread).

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** Wrap the synchronous LLM call (and the embedder call if synchronous) in `await asyncio.to_thread(...)` inside the async consume path. Keep `SalienceScorer._invoke_llm`'s await-detection intact; the `call_llm` passed from `_main` stays sync but is now run off-thread.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "fix(triage): run LLM/embed off the event loop (to_thread)"`

---

### Task 12: Thinking-mode response stripper (D3 belt-and-suspenders)

`enable_thinking=false` is the primary control; a response-side stripper guards GGUF templates that ignore the kwarg.

**Files:**
- Modify: `tradingagents/llm_clients/openai_client.py` (a small `local`-only subclass or a shared post-process), `tradingagents/sensing/salience.py`/`alert_evaluator.py` parse entry (strip before json.loads)
- Test: `tests/llm_clients/test_think_stripper.py` (create)

- [ ] **Step 1: Write the failing test** — feed content `"<think>reasoning…</think>{\"salience\": 0.9}"`; assert the stripped content parses cleanly and the L2 harness assertion "no `<think>` blocks" holds.

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement** a `strip_think_blocks(text)` helper (regex `<think>.*?</think>` non-greedy, DOTALL) applied on the classification parse paths only (not on synthesis paths, which never go local).

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(llm): strip stray <think> blocks on local classification paths"`

### ✅ Exit gate L1 (hardening complete, still on API)

- [ ] Salience + evaluator request a json_schema response format derived from their Pydantic models.
- [ ] Failure path no longer caches (no 0.1-salience-for-24h); failures surface as `deferred`/`parse_ok=False`, counted.
- [ ] `alert_evaluations` carries `model_id`, `parse_ok`, `latency_ms`; a query helper exposes the funnel.
- [ ] Triage consume loop never blocks on LLM/embed (verified by the non-blocking test).
- [ ] `<think>` blocks are stripped on the classification parse paths.
- [ ] `python -m pytest tests/sensing tests/orchestrator tests/llm_clients -q` all green. Ships value even if the local box never arrives.

---

## Phase L2 — Shadow evaluation (the gate that decides the model; needs the test box)

### Task 13: `shadow_eval` table + store helpers

**Files:**
- Modify: `tradingagents/persistence/schema.sql`, `tradingagents/persistence/store.py`
- Test: `tests/persistence/test_shadow_eval_store.py` (create)

- [ ] **Step 1: Write the failing test** — insert per-call rows and read them back; assert columns: `event_id`, `model_id`, `api_salience`, `local_salience`, `salience_delta`, `api_verdict`, `local_verdict`, `parse_ok`, `latency_ms`, `created_ts`.

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement** the `CREATE TABLE IF NOT EXISTS shadow_eval (...)` in `schema.sql` (+ index on `model_id`) and `insert_shadow_eval` / `fetch_shadow_eval` in `store.py`.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(persistence): shadow_eval table + store helpers"`

---

### Task 14: `scripts/shadow_eval.py` replay harness + report

Replays the last N (default 500) stored events/candidates through **both** the API quick model and the local endpoint, writes per-call `shadow_eval` rows, and prints the acceptance report.

**Files:**
- Create: `scripts/shadow_eval.py`, `tests/scripts/test_shadow_eval.py`

- [ ] **Step 1: Write the failing test** — drive the report function over synthetic `shadow_eval` rows and assert it computes: salience MAE; **threshold-crossing agreement** at 0.85 (salience) and 0.9 (evaluator confidence) operating points; evaluator verdict agreement as **Cohen's κ**; p50/p95 latency; parse-failure rate. Test the CLI flag surface (`--limit`, `--model`, `--persist-set`) via a dry run that doesn't hit the network.

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement** `shadow_eval.py`:
  - load last N events with stored raw text (reuse the promoter's raw-path read pattern);
  - build the API quick client (`create_role_llm` with global) and the local client (role override to `local` + chosen model);
  - score each through both; write `shadow_eval` rows;
  - print the report; `--persist-set` saves the replay set as the seed of the FORGE_04 Phase D labeled corpus (hand-label ~50 disagreements during review);
  - **run once per candidate** (`--model qwen3.6-27b…` then `--model deepseek-v4-flash-gguf…`) and let the numbers pick the model.

- [ ] **Step 4: Run green** → PASS (unit-level; real replay is a manual L2 run against the box).

- [ ] **Step 5: Commit** — `git commit -m "feat(eval): shadow_eval replay harness + acceptance report"`

### ✅ Exit gate L2 → L3 (acceptance, evaluated per candidate model)

Run `python scripts/shadow_eval.py --limit 500 --model <candidate> --persist-set` for **each** candidate, plus a 24h shadow soak. The chosen model must meet **all**:

- [ ] **Salience threshold-crossing agreement ≥ 95%** vs the API baseline at the live 0.85 operating point.
- [ ] **Evaluator verdict agreement ≥ 90%** vs API at the 0.9 operating point (report Cohen's κ alongside raw agreement).
- [ ] **Parse failures = 0** on the local path (grammar/json_schema-enforced).
- [ ] **p95 end-to-end latency ≤ current API p95** (per-call latency captured in `shadow_eval`).
- [ ] **24h shadow soak with zero endpoint-related triage stalls** (no blocked loop, no deferred backlog growth attributable to the endpoint).
- [ ] Disagreements reviewed; ~50 hand-labeled and persisted as the Phase D seed set.
- [ ] If **neither** candidate passes: stop at L2. The reconstruction still pays for itself via L0/L1 (D4/D6 + role mechanism), and the role wiring waits for a better model — do **not** force a cutover.

---

## Phase L3 — Cutover + availability policy (config flip; instantly revertible)

### Task 15: Availability policy — degrade loudly, fall back deliberately (D5)

**Files:**
- Modify: `tradingagents/orchestrator/promoter.py`, `tradingagents/sensing/triage.py`, `tradingagents/sensing/salience.py`
- Test: `tests/orchestrator/test_promoter_local_availability.py`, `tests/sensing/test_triage_local_availability.py` (create)

- [ ] **Step 1: Write the failing tests:**
  - **Startup probe:** with `fallback="none"` and a dead endpoint, both `_main`/`main` refuse to start the role (raise/exit) after an eager `/health` + 1-token completion probe; they log the resolved endpoint + model identity. With `fallback="api"`, they start and route to the global provider.
  - **Runtime (default `none`):** on per-call local failure, triage marks events `salience_source='deferred'` (un-scored, retryable — **not** 0.1), the promoter **skips the cycle**, and both **increment a failure counter**.
  - **`fallback="api"`:** after N consecutive failures, the role re-resolves to the global API provider under a hard daily call budget; the fallback path is logged and bounded.

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement.** Add the eager probe (mirror triage's eager embedder load — fail fast at startup). Thread the per-role `fallback` value through. On runtime failure: triage writes `salience_source='deferred'`; promoter skips + counts. Implement `fallback="api"` as a **second role resolution** (reuse `create_role_llm` with the global provider) gated by a consecutive-failure threshold + daily budget counter. Wire the failure counter to the self-alert seam (completed in L4).

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(availability): eager probe + loud degradation + deliberate api fallback"`

---

### Task 16: Cutover (config flip) + soak instrumentation

No code change beyond confirming the counters the soak reads. The flip is two env vars on the box.

**Files:**
- Modify: (none — config/env on the deployment box). Optionally extend `scripts/f4_f5_exit_gate.py` consumers to surface the new counters.
- Test: `tests/orchestrator/test_cutover_counters.py` (assert local-call volume, failure counter, and cost split are queryable for the soak report)

- [ ] **Step 1: Write the failing test** — assert the soak-report helper returns: local call volume, failure counter (expected 0), and gate/triage API spend (expected → 0 after cutover).

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement** the small report helper / extend the existing gate script to read the new telemetry.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(soak): cutover counters (local volume, failures, API spend->0)"`

- [ ] **Step 6: Flip on the box (deployment action, not a commit).** Set on `iic-triage`/`iic-promoter` only:
  `IIC_TRIAGE_LLM_PROVIDER=local`, `IIC_TRIAGE_LLM_MODEL=<chosen>`, `IIC_ALERT_GATE_LLM_PROVIDER=local`, `IIC_ALERT_GATE_LLM_MODEL=<chosen>`, `LOCAL_LLM_BASE_URL=http://<box>:8080/v1`. Revert = unset the two provider vars.

### ✅ Exit gate L3 (cutover accepted)

- [ ] Both roles resolve to `provider='local'` on the box; API path untouched and **instantly revertible** (unset two env vars).
- [ ] Startup probe passes; logs show resolved endpoint + model identity for both units.
- [ ] **72h soak** green via the existing F4/F5 exit gate **plus** new counters:
  - [ ] local call volume > 0 and tracking event rate;
  - [ ] failure counter = 0 (no endpoint-related stalls or deferred backlog);
  - [ ] cost panel shows gate/triage **API spend → 0** (local rows are `usd=0.0`, *free* not *unmetered*).
- [ ] No silent degradation observed: every skipped cycle / deferred score is counted and visible.

---

## Phase L4 — Ops hardening

### Task 17: Endpoint-down self-alert

**Files:**
- Modify: the alerting seam added by FORGE_04 Phase B (wire the failure counter to it); `tradingagents/orchestrator/promoter.py` / `tradingagents/sensing/triage.py`
- Test: `tests/orchestrator/test_endpoint_down_alert.py` (create)

- [ ] **Step 1: Write the failing test** — when the local endpoint failure counter crosses the threshold, a "local LLM endpoint down" self-alert is emitted through the operator channel exactly once (debounced), not per cycle.

- [ ] **Step 2: Run red** → FAIL.

- [ ] **Step 3: Implement** the debounced self-alert on the Phase B seam, fed by the D5 failure counter.

- [ ] **Step 4: Run green** → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(ops): self-alert when the local LLM endpoint is down"`

---

### Task 18: systemd env + runbook

**Files:**
- Modify: `ops/systemd/iic-triage.service`, `ops/systemd/iic-promoter.service`
- Create: `ops/runbooks/local-llm.md`

- [ ] **Step 1:** Add `LOCAL_LLM_BASE_URL` (+ optional `LOCAL_LLM_API_KEY`, `IIC_*_LLM_PROVIDER/MODEL`) to the `.env`/`Environment=` consumed by **only** the `iic-triage` and `iic-promoter` units. Do not touch worker/action-handler/morning units (those stay on API).

- [ ] **Step 2:** Write `ops/runbooks/local-llm.md` covering: probe commands (`curl /health`, 1-token completion), the fallback flip (`fallback: none → api`), the **model-swap procedure** (replace the GGUF + restart `llama-server` — no IIC code change; re-run the L0 contract test + L2 harness as the standing swap gate, never a hot swap), and the revert (unset two env vars).

- [ ] **Step 3: Commit** — `git commit -m "ops(local-llm): systemd env for triage/promoter + runbook"`

### ✅ Exit gate L4 (ops complete)

- [ ] Endpoint-down emits a debounced operator self-alert (tested).
- [ ] `LOCAL_LLM_BASE_URL` set on triage+promoter units only; other units unchanged.
- [ ] `ops/runbooks/local-llm.md` documents probe, fallback flip, model swap (= replace GGUF + restart, re-run contract + shadow gate), and revert.

---

## Sequencing & dependencies

- **FORGE_04 Phase A goes first.** This reconstruction is not the excuse that makes the 815-reject loop "fine because it's free now" — the terminal-state fix still lands. Locality changes latency/privacy, not correctness.
- **L0 + L1** are independent of the test box and can run in parallel with FORGE_04 Phase B; they ship value (D4 hardening, D6 telemetry, the role mechanism) even if the box never arrives.
- **L2** needs the box reachable on LAN. **L3** needs an L2-passed model. **L4** needs the FORGE_04 Phase B alerting seam.
- **Scope discipline:** do **not** move morning digest or refinement local "while we're at it" — those are synthesis-quality workloads; the role mechanism makes moving them later a config decision *once shadow-eval-grade evidence exists*.

## Risk → mitigation map (spec §4)

| Risk | Mitigation in this plan |
|---|---|
| Quality regression at the gate (27B quantized ≠ deepseek-v4-flash) | L2 exit gate with hard agreement thresholds + disagreement review; if neither candidate passes, stop at L2 (L0/L1 still pay off). |
| New failure domain (separate box, LAN) | D5 (Task 15) eager probe + loud per-cycle degradation + deliberate API fallback; L4 self-alert. Invariant: **no silent degradation** — every skip/defer is counted. |
| Concurrency ceiling (llama-server slots, not vLLM batching) | L1 `to_thread` (loop never blocks); `llama-server --parallel`; low post-dedupe event rate (85.5% dedupe); L2 measures worst-case double load honestly. |
| Template/quantization drift on model swap | L0 contract test + L2 harness re-run is the standing swap procedure (Task 18) — never a hot swap. |
| Scope creep (move digest/refinement local) | Risk-map row above; role mechanism defers any further split to a future evidence-backed config decision. |

## Self-Review Notes (spec → tasks)

- **D1 (first-class `local` provider)** → Tasks 1, 2 (provider, optional key, base URL + env override).
- **D2 (per-role routing)** → Tasks 4 (`llm_roles` + drop `classifier_llm`), 5 (`create_role_llm`), 7 (call-site swap).
- **D3 (capability rows + thinking-off)** → Tasks 3 (caps/catalog), 5 (`extra_body` passthrough), 6 (contract assertion), 12 (stripper).
- **D4 (structured-output hardening)** → Tasks 9 (salience schema + no fallback caching), 10 (evaluator schema + telemetry).
- **D5 (availability policy)** → Task 15 (probe, deferred/skip, counters, deliberate fallback).
- **D6 (telemetry + cost accounting)** → Tasks 8 (cost split, `usd=0.0`=free), 10 (latency/parse columns), 16 (soak counters).
- **D7 (cache discipline carries over)** → no rewrite; existing prompt-prefix regression tests bind unchanged (noted in File Structure / Task 9).
- **Migration L0–L4** → Phase headers above, each landing behind config with trunk releasable.
- **Exit gates** → per-phase ✅ checklists; the L2→L3 gate reproduces the spec's acceptance numbers (≥95% salience agreement, ≥90% evaluator, parse=0, p95 ≤ API p95, 24h zero-stall soak); L3 is the 72h soak via the existing gate + new counters.
- **FORGE_04 dependencies** → Phase A first (Sequencing); evaluator funnel telemetry (Task 10) closes the FORGE_04 "can't distinguish parse-fail from genuine reject" gap; shadow set (Task 14) seeds FORGE_04 Phase D; self-alert (Task 17) rides the FORGE_04 Phase B seam.
- **Type/name consistency:** role keys `"triage_salience"` / `"alert_gate"`, `create_role_llm(role, config)`, `salience_source='deferred'`, `fallback ∈ {"none","api"}`, columns `model_id`/`parse_ok`/`latency_ms`, table `shadow_eval`, and env vars `IIC_TRIAGE_LLM_*` / `IIC_ALERT_GATE_LLM_*` / `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY` are used identically across all tasks.
