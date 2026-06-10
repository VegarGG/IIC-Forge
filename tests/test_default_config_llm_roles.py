"""Tests for the llm_roles per-role routing block in DEFAULT_CONFIG.

Task: IIC-FORGE-05 Task 4 — llm_roles config block + env mapping; drop dead classifier_llm.
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_with_env(monkeypatch, **overrides):
    """Set/clear IIC_* env vars then reload default_config to re-evaluate DEFAULT_CONFIG."""
    import tradingagents.default_config as _dc
    _IIC_VARS = [
        "IIC_TRIAGE_LLM_PROVIDER",
        "IIC_TRIAGE_LLM_MODEL",
        "IIC_ALERT_GATE_LLM_PROVIDER",
        "IIC_ALERT_GATE_LLM_MODEL",
    ]
    for key in _IIC_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(_dc)


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_llm_roles_default_to_global():
    from tradingagents.default_config import DEFAULT_CONFIG as C
    roles = C["llm_roles"]
    for role in ("triage_salience", "alert_gate"):
        assert role in roles
        # Default ships with provider/model None so the role falls back to global.
        assert roles[role]["provider"] is None
        assert roles[role]["model"] is None
        assert roles[role]["fallback"] in ("none", "api")


@pytest.mark.unit
def test_llm_roles_has_expected_fields():
    """Each role entry must have the full set of routing fields."""
    from tradingagents.default_config import DEFAULT_CONFIG as C
    for role in ("triage_salience", "alert_gate"):
        entry = C["llm_roles"][role]
        assert "provider" in entry
        assert "model" in entry
        assert "base_url" in entry
        assert "extra_body" in entry
        assert "fallback" in entry


@pytest.mark.unit
def test_classifier_llm_key_removed():
    from tradingagents.default_config import DEFAULT_CONFIG as C
    assert "classifier_llm" not in C["refinement"]


# ---------------------------------------------------------------------------
# Env-override tests — cutover is a pure env flip on the production box
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_triage_llm_provider_env_override(monkeypatch):
    """IIC_TRIAGE_LLM_PROVIDER overrides llm_roles.triage_salience.provider."""
    dc = _reload_with_env(monkeypatch, IIC_TRIAGE_LLM_PROVIDER="local")
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["provider"] == "local"
    # Other roles must not be affected.
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["provider"] is None


@pytest.mark.unit
def test_triage_llm_model_env_override(monkeypatch):
    """IIC_TRIAGE_LLM_MODEL overrides llm_roles.triage_salience.model."""
    dc = _reload_with_env(monkeypatch, IIC_TRIAGE_LLM_MODEL="qwen3-8b")
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["model"] == "qwen3-8b"
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["model"] is None


@pytest.mark.unit
def test_alert_gate_llm_provider_env_override(monkeypatch):
    """IIC_ALERT_GATE_LLM_PROVIDER overrides llm_roles.alert_gate.provider."""
    dc = _reload_with_env(monkeypatch, IIC_ALERT_GATE_LLM_PROVIDER="openai")
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["provider"] == "openai"
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["provider"] is None


@pytest.mark.unit
def test_alert_gate_llm_model_env_override(monkeypatch):
    """IIC_ALERT_GATE_LLM_MODEL overrides llm_roles.alert_gate.model."""
    dc = _reload_with_env(monkeypatch, IIC_ALERT_GATE_LLM_MODEL="gpt-4o-mini")
    assert dc.DEFAULT_CONFIG["llm_roles"]["alert_gate"]["model"] == "gpt-4o-mini"
    assert dc.DEFAULT_CONFIG["llm_roles"]["triage_salience"]["model"] is None


@pytest.mark.unit
def test_combined_env_overrides_both_roles(monkeypatch):
    """All four IIC_* env vars can be set independently."""
    dc = _reload_with_env(
        monkeypatch,
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
def test_no_env_keeps_none_defaults(monkeypatch):
    """With no IIC_* env vars set, all role fields default to None."""
    dc = _reload_with_env(monkeypatch)
    for role in ("triage_salience", "alert_gate"):
        assert dc.DEFAULT_CONFIG["llm_roles"][role]["provider"] is None
        assert dc.DEFAULT_CONFIG["llm_roles"][role]["model"] is None
