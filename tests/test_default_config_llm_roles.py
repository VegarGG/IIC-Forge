"""Tests for the llm_roles per-role routing block in DEFAULT_CONFIG.

Task: IIC-FORGE-05 Task 4 — llm_roles config block + env mapping; drop dead classifier_llm.
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Env vars that may pollute the module if left set after a test
# ---------------------------------------------------------------------------

_IIC_VARS = [
    "IIC_TRIAGE_LLM_PROVIDER",
    "IIC_TRIAGE_LLM_MODEL",
    "IIC_ALERT_GATE_LLM_PROVIDER",
    "IIC_ALERT_GATE_LLM_MODEL",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def role_env(monkeypatch):
    """Reload-safe fixture for all llm_roles tests.

    Yields a callable ``_reload(**overrides)`` that:
      1. Clears all IIC_* env vars via monkeypatch.
      2. Applies the requested overrides.
      3. Reloads tradingagents.default_config and returns it.

    On teardown: monkeypatch.undo() restores the real env *before* the final
    reload so that the post-test module state reflects no overrides.
    """
    import tradingagents.default_config as _dc

    def _reload(**overrides):
        for key in _IIC_VARS:
            monkeypatch.delenv(key, raising=False)
        for key, val in overrides.items():
            monkeypatch.setenv(key, val)
        return importlib.reload(_dc)

    try:
        yield _reload
    finally:
        # undo() BEFORE reload so the reload sees the real environment.
        monkeypatch.undo()
        importlib.reload(_dc)


# ---------------------------------------------------------------------------
# Structure / default tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_llm_roles_default_to_global(role_env):
    dc = role_env()  # no overrides — committed defaults
    roles = dc.DEFAULT_CONFIG["llm_roles"]
    for role in ("triage_salience", "alert_gate"):
        assert role in roles
        # Default ships with provider/model None so the role falls back to global.
        assert roles[role]["provider"] is None
        assert roles[role]["model"] is None
        assert roles[role]["fallback"] in ("none", "api")


@pytest.mark.unit
def test_llm_roles_has_expected_fields(role_env):
    """Each role entry must have the full set of routing fields."""
    dc = role_env()  # no overrides — committed defaults
    for role in ("triage_salience", "alert_gate"):
        entry = dc.DEFAULT_CONFIG["llm_roles"][role]
        assert "provider" in entry
        assert "model" in entry
        assert "base_url" in entry
        assert "extra_body" in entry
        assert "fallback" in entry
        # Pin exact defaults Task 5 consumes.
        assert entry["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
        assert entry["fallback"] == "none"


@pytest.mark.unit
def test_classifier_llm_key_removed(role_env):
    dc = role_env()
    assert "classifier_llm" not in dc.DEFAULT_CONFIG["refinement"]


# ---------------------------------------------------------------------------
# Env-override tests — cutover is a pure env flip on the production box
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_triage_llm_provider_env_override(role_env):
    """IIC_TRIAGE_LLM_PROVIDER overrides llm_roles.triage_salience.provider."""
    dc = role_env(IIC_TRIAGE_LLM_PROVIDER="local")
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["provider"] == "local"
    # Other roles must not be affected.
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["provider"] is None


@pytest.mark.unit
def test_triage_llm_model_env_override(role_env):
    """IIC_TRIAGE_LLM_MODEL overrides llm_roles.triage_salience.model."""
    dc = role_env(IIC_TRIAGE_LLM_MODEL="qwen3-8b")
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["model"] == "qwen3-8b"
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["model"] is None


@pytest.mark.unit
def test_alert_gate_llm_provider_env_override(role_env):
    """IIC_ALERT_GATE_LLM_PROVIDER overrides llm_roles.alert_gate.provider."""
    dc = role_env(IIC_ALERT_GATE_LLM_PROVIDER="openai")
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["provider"] == "openai"
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["provider"] is None


@pytest.mark.unit
def test_alert_gate_llm_model_env_override(role_env):
    """IIC_ALERT_GATE_LLM_MODEL overrides llm_roles.alert_gate.model."""
    dc = role_env(IIC_ALERT_GATE_LLM_MODEL="gpt-4o-mini")
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["model"] == "gpt-4o-mini"
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["model"] is None


@pytest.mark.unit
def test_combined_env_overrides_both_roles(role_env):
    """All four IIC_* env vars can be set independently."""
    dc = role_env(
        IIC_TRIAGE_LLM_PROVIDER="local",
        IIC_TRIAGE_LLM_MODEL="qwen3-8b",
        IIC_ALERT_GATE_LLM_PROVIDER="local",
        IIC_ALERT_GATE_LLM_MODEL="qwen3-4b",
    )
    triage = dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]
    gate = dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]
    assert triage["provider"] == "local"
    assert triage["model"] == "qwen3-8b"
    assert gate["provider"] == "local"
    assert gate["model"] == "qwen3-4b"


@pytest.mark.unit
def test_no_env_keeps_none_defaults(role_env):
    """With no IIC_* env vars set, all role fields default to None."""
    dc = role_env()
    for role in ("triage_salience", "alert_gate"):
        assert dc.DEFAULT_CONFIG["llm_roles"][role]["provider"] is None
        assert dc.DEFAULT_CONFIG["llm_roles"][role]["model"] is None
