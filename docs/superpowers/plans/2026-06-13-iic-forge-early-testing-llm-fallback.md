# Early-Testing Local-LLM Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing local→cloud classification fallback safe and turnkey for early testing, using a dedicated, removable `IIC_LLM_FALLBACK_API_KEY` that is structurally isolated from the workers' persistent `DEEPSEEK_API_KEY`.

**Architecture:** The two classification roles (`triage_salience`, `alert_gate`) already fall back from a local model to the global cloud provider via `availability.resolve_role_llm_global`. We (1) let `OpenAIClient.get_llm` honor an explicit `api_key`, (2) thread an explicit key through `create_role_llm`, (3) make `resolve_role_llm_global` inject `IIC_LLM_FALLBACK_API_KEY` and *refuse* when it is absent (never borrowing the worker key), (4) add a startup guardrail that loudly warns when `fallback=api` can never fire (budget ≤ 0 or key missing), and (5) wire that guardrail into both daemons. Docs get a copy-paste testing recipe + teardown.

**Tech Stack:** Python 3.13, pytest, langchain-openai, the repo's existing `tradingagents.llm_clients` + sensing/orchestrator daemons.

**Spec:** `docs/superpowers/specs/2026-06-13-iic-forge-early-testing-llm-fallback-design.md`

**Branch:** `feat/early-testing-llm-fallback` (already checked out).

**Conventions:** Run tests with `python -m pytest`. The autouse fixture in `tests/conftest.py` injects placeholder provider keys; tests that need a key *absent* must `monkeypatch.delenv(...)`. Commit after every task.

---

### Task 1: Explicit `api_key` precedence in `OpenAIClient.get_llm`

Make an explicit `api_key` kwarg win over the provider-mapped env var and skip the missing-env-var raise. This is the seam that lets one process use different keys for different clients.

**Files:**
- Modify: `tradingagents/llm_clients/openai_client.py:216-235`
- Test: `tests/llm_clients/test_fallback_key_isolation.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/llm_clients/test_fallback_key_isolation.py`:

```python
"""Isolated-key behaviour for the early-testing classification fallback.

Covers: explicit-api_key precedence in OpenAIClient.get_llm; create_role_llm
forwarding an explicit key; resolve_role_llm_global injecting
IIC_LLM_FALLBACK_API_KEY and refusing when it is absent (never borrowing the
worker's DEEPSEEK_API_KEY); the startup guardrail helper.
"""

from __future__ import annotations

import logging

import pytest

from tradingagents.llm_clients.factory import create_llm_client


def test_explicit_api_key_overrides_env_and_skips_raise(monkeypatch):
    # DEEPSEEK_API_KEY absent: without an explicit key get_llm() would raise.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    llm = create_llm_client(
        provider="deepseek", model="deepseek-chat", api_key="sk-explicit"
    ).get_llm()
    assert llm.openai_api_key.get_secret_value() == "sk-explicit"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py::test_explicit_api_key_overrides_env_and_skips_raise -v`
Expected: FAIL with `ValueError: API key for provider 'deepseek' is not set` (the env read raises before the explicit key is applied).

- [ ] **Step 3: Write minimal implementation**

In `tradingagents/llm_clients/openai_client.py`, replace the `if self.provider in _PROVIDER_BASE_URL:` block (currently lines 216-235):

```python
        if self.provider in _PROVIDER_BASE_URL:
            llm_kwargs["base_url"] = self.base_url or _resolve_provider_base_url(self.provider)
            api_key_env = get_api_key_env(self.provider)
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
                elif is_optional_key(self.provider):
                    # Optional-key providers (e.g. local/llama-server on a LAN)
                    # work without authentication; fall through to the sentinel
                    # so ChatOpenAI does not complain about a missing key.
                    llm_kwargs["api_key"] = "local"
                else:
                    raise ValueError(
                        f"API key for provider '{self.provider}' is not set. "
                        f"Please set the {api_key_env} environment variable "
                        f"(e.g. add {api_key_env}=your_key to your .env file)."
                    )
            else:
                llm_kwargs["api_key"] = "ollama"
```

with:

```python
        if self.provider in _PROVIDER_BASE_URL:
            llm_kwargs["base_url"] = self.base_url or _resolve_provider_base_url(self.provider)
            # An explicit api_key (e.g. the isolated classification-fallback key
            # injected by availability.resolve_role_llm_global) takes precedence
            # over the provider-mapped env var and skips the missing-env-var
            # raise. This lets one process authenticate different clients with
            # different keys (the worker's DEEPSEEK_API_KEY vs. a throwaway
            # classification-fallback key) without sharing credentials.
            explicit_key = self.kwargs.get("api_key")
            if explicit_key:
                llm_kwargs["api_key"] = explicit_key
            else:
                api_key_env = get_api_key_env(self.provider)
                if api_key_env:
                    api_key = os.environ.get(api_key_env)
                    if api_key:
                        llm_kwargs["api_key"] = api_key
                    elif is_optional_key(self.provider):
                        # Optional-key providers (e.g. local/llama-server on a
                        # LAN) work without authentication; fall through to the
                        # sentinel so ChatOpenAI does not complain.
                        llm_kwargs["api_key"] = "local"
                    else:
                        raise ValueError(
                            f"API key for provider '{self.provider}' is not set. "
                            f"Please set the {api_key_env} environment variable "
                            f"(e.g. add {api_key_env}=your_key to your .env file)."
                        )
                else:
                    llm_kwargs["api_key"] = "ollama"
```

(The existing `_PASSTHROUGH_KWARGS` loop at lines 240-242 still forwards the same truthy `api_key` afterward — a harmless idempotent re-set.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py::test_explicit_api_key_overrides_env_and_skips_raise -v`
Expected: PASS

- [ ] **Step 5: Run the client regression suite**

Run: `python -m pytest tests/llm_clients/test_local_provider.py tests/llm_clients/test_create_role_llm.py -q`
Expected: PASS (no behaviour change when no explicit key is passed).

- [ ] **Step 6: Commit**

```bash
git add tradingagents/llm_clients/openai_client.py tests/llm_clients/test_fallback_key_isolation.py
git commit -m "feat(llm): explicit api_key wins over provider env var in get_llm"
```

---

### Task 2: `create_role_llm` forwards an explicit `api_key`

**Files:**
- Modify: `tradingagents/llm_clients/factory.py:63` (signature) and `:126-133` (build)
- Test: `tests/llm_clients/test_fallback_key_isolation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/llm_clients/test_fallback_key_isolation.py`:

```python
def _cfg_global_deepseek():
    return {
        "llm_provider": "deepseek",
        "quick_think_llm": "deepseek-chat",
        "backend_url": None,
        "llm_roles": {
            "triage_salience": {"provider": None, "model": None,
                                "base_url": None, "extra_body": None,
                                "fallback": "api"},
        },
    }


def test_create_role_llm_forwards_explicit_api_key(monkeypatch):
    from tradingagents.llm_clients.factory import create_role_llm
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = create_role_llm(
        "triage_salience", _cfg_global_deepseek(), api_key="sk-role")
    assert client.get_llm().openai_api_key.get_secret_value() == "sk-role"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py::test_create_role_llm_forwards_explicit_api_key -v`
Expected: FAIL with `TypeError: create_role_llm() got an unexpected keyword argument 'api_key'`.

- [ ] **Step 3: Write minimal implementation**

In `tradingagents/llm_clients/factory.py`, change the signature (line 63):

```python
def create_role_llm(role: str, config: Dict[str, Any]) -> BaseLLMClient:
```

to:

```python
def create_role_llm(
    role: str, config: Dict[str, Any], *, api_key: Optional[str] = None
) -> BaseLLMClient:
```

Then in the build block (currently lines 126-133), replace:

```python
    extra_body = override.get("extra_body")
    kwargs = {}
    if extra_body:
        kwargs["extra_body"] = copy.deepcopy(extra_body)

    return create_llm_client(
        provider=provider, model=model, base_url=base_url, **kwargs
    )
```

with:

```python
    extra_body = override.get("extra_body")
    kwargs = {}
    if extra_body:
        kwargs["extra_body"] = copy.deepcopy(extra_body)
    if api_key:
        # Explicit per-client key (e.g. the isolated classification-fallback
        # key). Forwarded only when truthy so normal env-based resolution is
        # untouched for every existing caller.
        kwargs["api_key"] = api_key

    return create_llm_client(
        provider=provider, model=model, base_url=base_url, **kwargs
    )
```

(`Optional` is already imported in `factory.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py::test_create_role_llm_forwards_explicit_api_key -v`
Expected: PASS

- [ ] **Step 5: Run the factory regression suite**

Run: `python -m pytest tests/llm_clients/test_create_role_llm.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/llm_clients/factory.py tests/llm_clients/test_fallback_key_isolation.py
git commit -m "feat(llm): create_role_llm forwards an optional explicit api_key"
```

---

### Task 3: `resolve_role_llm_global` injects the dedicated key / refuses when absent

The classification fallback now authenticates ONLY with `IIC_LLM_FALLBACK_API_KEY`; absent it, the fallback refuses rather than borrowing `DEEPSEEK_API_KEY`. The conftest placeholder keeps existing fallback tests green.

**Files:**
- Modify: `tradingagents/llm_clients/availability.py:346-364`
- Modify: `tests/conftest.py:14-28` (add the placeholder env var)
- Test: `tests/llm_clients/test_fallback_key_isolation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/llm_clients/test_fallback_key_isolation.py`:

```python
def test_resolve_role_llm_global_injects_fallback_key(monkeypatch):
    from tradingagents.llm_clients.availability import resolve_role_llm_global
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("IIC_LLM_FALLBACK_API_KEY", "sk-fallback")
    client = resolve_role_llm_global("triage_salience", _cfg_global_deepseek())
    assert client.get_llm().openai_api_key.get_secret_value() == "sk-fallback"


def test_resolve_role_llm_global_refuses_without_key(monkeypatch):
    from tradingagents.llm_clients.availability import (
        LocalEndpointUnavailable, resolve_role_llm_global,
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "worker-key")
    monkeypatch.delenv("IIC_LLM_FALLBACK_API_KEY", raising=False)
    with pytest.raises(LocalEndpointUnavailable, match="IIC_LLM_FALLBACK_API_KEY"):
        resolve_role_llm_global("triage_salience", _cfg_global_deepseek())


def test_fallback_key_isolated_from_worker_key(monkeypatch):
    """The worker client uses DEEPSEEK_API_KEY; the classification fallback
    uses IIC_LLM_FALLBACK_API_KEY. The two never share a credential."""
    from tradingagents.llm_clients.availability import resolve_role_llm_global
    monkeypatch.setenv("DEEPSEEK_API_KEY", "worker-key")
    monkeypatch.setenv("IIC_LLM_FALLBACK_API_KEY", "test-key")
    worker = create_llm_client(provider="deepseek", model="deepseek-chat").get_llm()
    fallback = resolve_role_llm_global(
        "triage_salience", _cfg_global_deepseek()).get_llm()
    assert worker.openai_api_key.get_secret_value() == "worker-key"
    assert fallback.openai_api_key.get_secret_value() == "test-key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py -k resolve_role_llm_global -v`
Expected: FAIL — `injects_fallback_key` raises `ValueError` (DEEPSEEK unset, no injection yet); `refuses_without_key` does NOT raise `LocalEndpointUnavailable` (it currently borrows the worker key).

- [ ] **Step 3: Implement the resolver change**

In `tradingagents/llm_clients/availability.py`, replace `resolve_role_llm_global` (lines 346-364) — keep the docstring, insert the key gate and pass `api_key=`:

```python
def resolve_role_llm_global(role: str, config: Dict[str, Any]):
    """Second role resolution: the GLOBAL API provider (override stripped).

    Used for both the startup fallback (dead probe + fallback="api") and the
    runtime fallback (consecutive-failure threshold crossed).  Loudly logged.

    The classification fallback authenticates ONLY with the dedicated,
    removable ``IIC_LLM_FALLBACK_API_KEY`` — it never borrows the worker's
    global provider key (e.g. ``DEEPSEEK_API_KEY``).  Absent that key the
    fallback is unavailable: we raise rather than silently sharing a credential
    (structural isolation, design 2026-06-13).
    """
    fallback_key = os.environ.get("IIC_LLM_FALLBACK_API_KEY")
    if not fallback_key:
        raise LocalEndpointUnavailable(
            f"role {role}: fallback=api engaged but IIC_LLM_FALLBACK_API_KEY "
            f"is not set; refusing to borrow the global provider key"
        )
    # Module-attribute access (not from-import) so tests that patch
    # ``factory.create_role_llm`` intercept this call too.
    from tradingagents.llm_clients import factory
    client = factory.create_role_llm(
        role, strip_role_override(config, role), api_key=fallback_key)
    # getattr-defensive: tests patch create_role_llm with minimal fakes that
    # may lack base_url/get_provider_name.
    provider_name = getattr(client, "get_provider_name", lambda: "?")()
    log.warning(
        "role %s re-resolved to GLOBAL provider (fallback=api): "
        "provider=%s model=%s base_url=%s",
        role, provider_name, client.model, getattr(client, "base_url", None),
    )
    return client
```

- [ ] **Step 4: Add the placeholder key to the autouse fixture**

In `tests/conftest.py`, add the new var to `_API_KEY_ENV_VARS` (after `"ALPHA_VANTAGE_API_KEY",` on line 28):

```python
    "ALPHA_VANTAGE_API_KEY",
    "IIC_LLM_FALLBACK_API_KEY",
```

This makes every test see a placeholder fallback key by default, so the existing `fallback="api"` availability tests keep resolving. Tests that assert the *absence* of the key `monkeypatch.delenv` it explicitly (as the new tests above do).

- [ ] **Step 5: Run the new tests + the existing fallback suites**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py tests/sensing/test_triage_local_availability.py tests/orchestrator/test_promoter_local_availability.py -q`
Expected: PASS (new isolation tests green; existing fallback=api tests green via the placeholder key).

- [ ] **Step 6: Commit**

```bash
git add tradingagents/llm_clients/availability.py tests/conftest.py tests/llm_clients/test_fallback_key_isolation.py
git commit -m "feat(llm): isolate classification fallback to IIC_LLM_FALLBACK_API_KEY; refuse when absent"
```

---

### Task 4: `warn_if_fallback_unsatisfiable` startup guardrail helper

A pure helper that loudly warns when `fallback=api` can never fire (budget ≤ 0 or dedicated key missing).

**Files:**
- Modify: `tradingagents/llm_clients/availability.py` (add helper near the other resolution helpers, after `resolve_role_llm_with_fallback`)
- Test: `tests/llm_clients/test_fallback_key_isolation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/llm_clients/test_fallback_key_isolation.py`:

```python
_LOG = logging.getLogger("test.fallback.guardrail")


def test_guardrail_warns_when_api_and_budget_zero(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("triage_salience", "api", 0,
                                   fallback_key_present=True, log=_LOG)
    assert "triage_salience" in caplog.text
    assert "budget" in caplog.text.lower()


def test_guardrail_warns_when_api_and_key_missing(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("alert_gate", "api", 500,
                                   fallback_key_present=False, log=_LOG)
    assert "alert_gate" in caplog.text
    assert "IIC_LLM_FALLBACK_API_KEY" in caplog.text


def test_guardrail_silent_when_satisfiable(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("triage_salience", "api", 500,
                                   fallback_key_present=True, log=_LOG)
    assert caplog.text == ""


def test_guardrail_silent_when_fallback_none(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("triage_salience", "none", 0,
                                   fallback_key_present=False, log=_LOG)
    assert caplog.text == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py -k guardrail -v`
Expected: FAIL with `ImportError: cannot import name 'warn_if_fallback_unsatisfiable'`.

- [ ] **Step 3: Write minimal implementation**

In `tradingagents/llm_clients/availability.py`, add after `resolve_role_llm_with_fallback` (after line 432):

```python
def warn_if_fallback_unsatisfiable(role, fallback_mode, max_per_day, *,
                                   fallback_key_present, log):
    """Loudly warn at startup when ``fallback=api`` can never actually fire.

    Two unsatisfiable configs are surfaced (either or both):
      - the per-UTC-day budget is non-positive (every fallback call is denied);
      - the dedicated ``IIC_LLM_FALLBACK_API_KEY`` is absent (the fallback
        refuses rather than borrowing the worker key).

    Every other combination — including the fail-closed production default
    (``fallback="none"``) — is a silent no-op.
    """
    if (fallback_mode or "none").lower() != "api":
        return
    if max_per_day <= 0:
        log.warning(
            "role %s: fallback=api but daily budget is %s (<=0); the fallback "
            "will NEVER fire. Set IIC_LLM_FALLBACK_DAILY_BUDGET > 0 to enable it.",
            role, max_per_day,
        )
    if not fallback_key_present:
        log.warning(
            "role %s: fallback=api but IIC_LLM_FALLBACK_API_KEY is not set; the "
            "fallback will REFUSE (it never borrows the worker key). Set "
            "IIC_LLM_FALLBACK_API_KEY to enable it.",
            role,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/llm_clients/test_fallback_key_isolation.py -k guardrail -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add tradingagents/llm_clients/availability.py tests/llm_clients/test_fallback_key_isolation.py
git commit -m "feat(llm): add warn_if_fallback_unsatisfiable startup guardrail"
```

---

### Task 5: Wire the guardrail into `promoter._main`

Promoter already reads `role_cfg`/`fallback_mode` before resolution; add `import os`, compute the budget once, and call the guardrail before the probe.

**Files:**
- Modify: `tradingagents/orchestrator/promoter.py` (imports; `main` lines ~170-231)
- Test: `tests/orchestrator/test_promoter_local_availability.py`

- [ ] **Step 1: Write the failing wiring test**

Append to `tests/orchestrator/test_promoter_local_availability.py`:

```python
@pytest.mark.unit
def test_main_warns_and_refuses_when_fallback_key_missing(
    tmp_path, caplog, monkeypatch
):
    from tradingagents.llm_clients.availability import LocalEndpointUnavailable
    from tradingagents.orchestrator.promoter import main

    caplog.set_level(logging.INFO)
    monkeypatch.delenv("IIC_LLM_FALLBACK_API_KEY", raising=False)
    dead = _dead_base_url()
    cfg = _cfg(tmp_path, gate_role=_gate_role(
        provider="local", model="local-gate-model",
        base_url=dead, fallback="api"))

    with pytest.raises(LocalEndpointUnavailable):
        main(config=cfg)

    # The guardrail fired at startup (before resolution) and the fallback
    # refused rather than borrowing the worker key.
    assert "alert_gate" in caplog.text
    assert "IIC_LLM_FALLBACK_API_KEY" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/orchestrator/test_promoter_local_availability.py::test_main_warns_and_refuses_when_fallback_key_missing -v`
Expected: FAIL — it raises `LocalEndpointUnavailable` (the resolver refuse already works) but `caplog` lacks the guardrail line `IIC_LLM_FALLBACK_API_KEY` because the guardrail is not wired in yet. (The assertion on caplog fails.)

- [ ] **Step 3: Add `import os` to promoter**

In `tradingagents/orchestrator/promoter.py`, add `import os` to the stdlib import block (after line 9 `import json`):

```python
import json
import os
```

- [ ] **Step 4: Import the guardrail and call it before resolution**

In `promoter.py`, extend the availability import (lines 170-175) to include `warn_if_fallback_unsatisfiable`:

```python
    from tradingagents.llm_clients.availability import (
        PROMOTER_FAILURE_COUNTER, PROMOTER_FALLBACK_BUDGET,
        TRANSPORT_EXCEPTIONS, AvailabilityCounter, DailyFallbackBudget,
        LocalEndpointUnavailable, resolve_role_llm_global,
        resolve_role_llm_with_fallback, warn_if_fallback_unsatisfiable,
    )
```

Then, immediately after line 198 (`fallback_threshold = int(role_cfg.get("fallback_threshold", 3))`), insert:

```python
    fallback_max_per_day = int(role_cfg.get("fallback_daily_budget", 500))
    warn_if_fallback_unsatisfiable(
        "alert_gate", fallback_mode, fallback_max_per_day,
        fallback_key_present=bool(os.environ.get("IIC_LLM_FALLBACK_API_KEY")),
        log=log,
    )
```

- [ ] **Step 5: Reuse the computed budget**

In `promoter.py`, change the `DailyFallbackBudget` construction (line 229) from:

```python
        max_per_day=int(role_cfg.get("fallback_daily_budget", 500)),
```

to:

```python
        max_per_day=fallback_max_per_day,
```

- [ ] **Step 6: Run the wiring test + promoter suite**

Run: `python -m pytest tests/orchestrator/test_promoter_local_availability.py tests/orchestrator/test_promoter_uses_role.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tradingagents/orchestrator/promoter.py tests/orchestrator/test_promoter_local_availability.py
git commit -m "feat(promoter): warn at startup when fallback=api is unsatisfiable"
```

---

### Task 6: Wire the guardrail into `triage._main`

Triage resolves *before* it reads `role_cfg`, so hoist the `role_cfg`/`fallback_mode`/budget reads above the resolution call and remove the now-duplicate reads.

**Files:**
- Modify: `tradingagents/sensing/triage.py:638-709`
- Test: `tests/sensing/test_triage_local_availability.py`

- [ ] **Step 1: Write the failing wiring test**

Append to `tests/sensing/test_triage_local_availability.py`:

```python
@pytest.mark.unit
def test_main_warns_and_refuses_when_fallback_key_missing(
    monkeypatch, tmp_path, caplog
):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.availability import LocalEndpointUnavailable

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()

    import tradingagents.sensing.redis_client as redis_mod
    monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())
    import tradingagents.sensing.embeddings as emb_mod

    class _FakeEmbedder:
        def load(self):
            pass

    monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                        lambda model: _FakeEmbedder())

    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("IIC_LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", str(tmp_path / "iic.db"))
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
        "triage_salience": _role_entry(
            provider="local", model="test-local-model",
            base_url=dead, fallback="api"),
        "alert_gate": _role_entry(),
    })

    from tradingagents.sensing.triage import _main
    with pytest.raises(LocalEndpointUnavailable):
        _main()

    assert "triage_salience" in caplog.text
    assert "IIC_LLM_FALLBACK_API_KEY" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/sensing/test_triage_local_availability.py::test_main_warns_and_refuses_when_fallback_key_missing -v`
Expected: FAIL — `_main` raises `LocalEndpointUnavailable` (resolver refuse) but `caplog` lacks the `IIC_LLM_FALLBACK_API_KEY` guardrail line (guardrail not wired + not run before resolution yet).

- [ ] **Step 3: Import the guardrail**

In `tradingagents/sensing/triage.py`, extend the availability import (lines 638-642) to include `warn_if_fallback_unsatisfiable`:

```python
    from tradingagents.llm_clients.availability import (
        TRIAGE_FAILURE_COUNTER, TRIAGE_FALLBACK_BUDGET,
        AvailabilityCounter, DailyFallbackBudget, LocalEndpointUnavailable,
        resolve_role_llm_global, resolve_role_llm_with_fallback,
        warn_if_fallback_unsatisfiable,
    )
```

- [ ] **Step 4: Hoist the role-config reads and call the guardrail before resolution**

In `triage.py`, immediately after the `from tradingagents.sensing.salience import maybe_bind_salience_schema` line (line 643) and **before** `quick_client, used_fallback = resolve_role_llm_with_fallback(...)` (line 644), insert:

```python
    role_cfg = C.get("llm_roles", {}).get("triage_salience", {}) or {}
    fallback_mode = (role_cfg.get("fallback") or "none").lower()
    fallback_max_per_day = int(role_cfg.get("fallback_daily_budget", 500))
    warn_if_fallback_unsatisfiable(
        "triage_salience", fallback_mode, fallback_max_per_day,
        fallback_key_present=bool(os.environ.get("IIC_LLM_FALLBACK_API_KEY")),
        log=log,
    )
```

- [ ] **Step 5: Remove the now-duplicate reads**

In `triage.py`, delete the two now-duplicate assignments (currently lines 653-654):

```python
    role_cfg = C.get("llm_roles", {}).get("triage_salience", {}) or {}
    fallback_mode = (role_cfg.get("fallback") or "none").lower()
```

Leave the line that follows it intact:

```python
    fallback_threshold = int(role_cfg.get("fallback_threshold", 3))
```

(`role_cfg` is now the hoisted variable.)

- [ ] **Step 6: Reuse the computed budget**

In `triage.py`, change the `DailyFallbackBudget` construction (line 707) from:

```python
        max_per_day=int(role_cfg.get("fallback_daily_budget", 500)),
```

to:

```python
        max_per_day=fallback_max_per_day,
```

- [ ] **Step 7: Run the wiring test + triage suites**

Run: `python -m pytest tests/sensing/test_triage_local_availability.py tests/sensing/test_triage_main_uses_role.py tests/llm_clients/test_json_schema_binding.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add tradingagents/sensing/triage.py tests/sensing/test_triage_local_availability.py
git commit -m "feat(triage): warn at startup when fallback=api is unsatisfiable"
```

---

### Task 7: Documentation — testing recipe + teardown + pointer

**Files:**
- Modify: `ops/runbooks/local-llm.md` (new subsection after section 3)
- Modify: `ops/runbooks/operating-guide.md` (pointer in §4.7)

- [ ] **Step 1: Add the early-testing recipe to `local-llm.md`**

In `ops/runbooks/local-llm.md`, after section 3 (the `## 3. Fallback flip ...` block, before `## 4. Model-swap procedure`), insert:

```markdown
---

## 3b. Early-testing fallback (isolated, removable key)

For the early-testing phase you can let triage/promoter keep running on the
cloud when the local model is down, using a **dedicated, throwaway key** that is
structurally separate from the workers' persistent `DEEPSEEK_API_KEY`. Set these
in your **private `.env` only** — never in `ops/env.iic-forge.example`:

```dotenv
IIC_LLM_FALLBACK_MODE=api            # both classification roles → cloud on local outage
IIC_LLM_FALLBACK_DAILY_BUDGET=500    # hard per-UTC-day cap (compiled default; tune to taste)
IIC_LLM_FALLBACK_API_KEY=<throwaway-testing-key>   # SEPARATE from DEEPSEEK_API_KEY
# DEEPSEEK_API_KEY stays the workers' persistent key — do NOT reuse it above.
```

Activate and verify:

```bash
docker compose restart triage promoter
docker compose logs --tail=20 triage   | grep -E 'resolved:|fallback'
docker compose logs --tail=20 promoter | grep -E 'resolved:|fallback'
```

The classification fallback authenticates **only** with `IIC_LLM_FALLBACK_API_KEY`.
If it is unset while `IIC_LLM_FALLBACK_MODE=api`, the fallback **refuses** (it
never borrows the worker key) and each daemon logs a startup warning naming the
missing variable. The same warning fires if `IIC_LLM_FALLBACK_DAILY_BUDGET=0`.

**Post-deployment teardown:** remove `IIC_LLM_FALLBACK_API_KEY` *and* set
`IIC_LLM_FALLBACK_MODE=none` (or drop it), then `docker compose restart triage
promoter`. Either lock alone severs the classification fallback; the workers'
`DEEPSEEK_API_KEY` is untouched.
```

- [ ] **Step 2: Add a pointer in `operating-guide.md`**

In `ops/runbooks/operating-guide.md`, in section `### 4.7 Local LLM operation`, after the two existing bullets (`- After any local-LLM .env change ...` and `- IIC_LLM_FALLBACK_MODE=none ...`), add a third bullet:

```markdown
- For early testing you can enable cloud fallback with an **isolated, removable**
  key (`IIC_LLM_FALLBACK_API_KEY`, separate from the workers' `DEEPSEEK_API_KEY`).
  See `local-llm.md` §3b for the recipe and the post-deployment teardown.
```

- [ ] **Step 3: Commit**

```bash
git add ops/runbooks/local-llm.md ops/runbooks/operating-guide.md
git commit -m "docs(ops): early-testing fallback recipe with isolated key + teardown"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the LLM + sensing + orchestrator suites**

Run: `python -m pytest tests/llm_clients tests/sensing tests/orchestrator -q`
Expected: PASS (no failures, no errors).

- [ ] **Step 2: Run the env-overrides + default-config suites (touch the same config seams)**

Run: `python -m pytest tests/test_env_overrides.py tests/test_default_config_llm_roles.py -q`
Expected: PASS

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests -q`
Expected: PASS. If any unrelated pre-existing failure appears, confirm it also fails on `main` before treating it as in-scope.

- [ ] **Step 4: Final confirmation commit (only if any cleanup was needed)**

If steps 1-3 required no changes, skip. Otherwise commit the fix with a descriptive message.

---

## Self-Review

**Spec coverage:**
- §5.1 dedicated key `IIC_LLM_FALLBACK_API_KEY` → Task 3 (read in resolver) + Task 7 (docs) + Task 3 Step 4 (conftest placeholder).
- §5.2 resolver injects key / refuses when absent → Task 3.
- §5.3 client honors explicit api_key → Task 1.
- §5.4 startup guardrail (warn, budget≤0 or key missing, before resolution) → Task 4 (helper) + Tasks 5-6 (wiring before resolution).
- §5.5 testing recipe + teardown → Task 7.
- §5.6 tests (key isolation, refuse-on-missing, explicit-key precedence, guardrail, engagement) → Tasks 1-6; the engagement path is exercised by the existing `test_runtime_fallback_api_engages_after_threshold` / `test_main_*_fallback_api_*` suites kept green via the conftest placeholder (Task 3 Step 5).
- §7 acceptance criteria 1-6 → Tasks 3, 5, 6 (refuse + warn), Task 8 (suite green), conftest (template untouched ⇒ contract test unaffected; no edit to `ops/env.iic-forge.example`).
- §8 files touched → Tasks 1-7 cover every listed file; no edit to `ops/env.iic-forge.example`, `compose.yml`, `default_config.py` defaults, or the worker path.

**Placeholder scan:** none — every code/test step carries complete code; `<throwaway-testing-key>` is a doc placeholder for an operator secret, not a plan gap.

**Type/name consistency:** `warn_if_fallback_unsatisfiable(role, fallback_mode, max_per_day, *, fallback_key_present, log)` — same signature in Task 4 (definition), Task 5, and Task 6 (calls). `create_role_llm(..., *, api_key=None)` — defined Task 2, called with `api_key=` in Task 3. `IIC_LLM_FALLBACK_API_KEY` and `fallback_max_per_day` spelled identically across tasks.
