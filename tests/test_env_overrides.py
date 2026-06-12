"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import importlib

import pytest

import tradingagents.default_config as default_config_module


def _reload_with_env(monkeypatch, **overrides):
    """Set/clear env vars then reload default_config to re-evaluate DEFAULT_CONFIG."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


def test_no_env_uses_built_in_defaults(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["llm_provider"] == "deepseek"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "deepseek-v4-pro"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "deepseek-v4-flash"
    assert dc.DEFAULT_CONFIG["backend_url"] is None
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "google"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gemini-3-pro-preview"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gemini-3-flash-preview"
    assert dc.DEFAULT_CONFIG["backend_url"] == "https://example.invalid/v1"
    assert dc.DEFAULT_CONFIG["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3
    assert isinstance(dc.DEFAULT_CONFIG["max_debate_rounds"], int)
    assert dc.DEFAULT_CONFIG["max_risk_discuss_rounds"] == 2
    assert isinstance(dc.DEFAULT_CONFIG["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is expected


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "deepseek"
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError):
        importlib.reload(default_config_module)
    # Restore module state for subsequent tests in this process
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    importlib.reload(default_config_module)


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in dc.DEFAULT_CONFIG


def test_telegram_sensing_channels_override(monkeypatch):
    """TELEGRAM_SENSING_CHANNELS (.env) populates telegram_channels: comma-split,
    whitespace-trimmed, leading '@' stripped, empty tokens dropped."""
    dc = _reload_with_env(
        monkeypatch,
        TELEGRAM_SENSING_CHANNELS=" @FirstSquawk, DeItaone ,@WatcherGuru,",
    )
    assert dc.DEFAULT_CONFIG["telegram_channels"] == [
        "FirstSquawk", "DeItaone", "WatcherGuru",
    ]

    # Unset → committed default ([] = listen to nothing).
    monkeypatch.delenv("TELEGRAM_SENSING_CHANNELS", raising=False)
    dc = importlib.reload(default_config_module)
    assert dc.DEFAULT_CONFIG["telegram_channels"] == []


# ---------------------------------------------------------------------------
# Fix A1: TRADINGAGENTS_SENSING_REDIS_URL → config["sensing_redis_url"]
# ---------------------------------------------------------------------------

def test_sensing_redis_url_override(monkeypatch):
    """TRADINGAGENTS_SENSING_REDIS_URL must land in config['sensing_redis_url']."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_SENSING_REDIS_URL="redis://redis:6379/0",
    )
    assert dc.DEFAULT_CONFIG["sensing_redis_url"] == "redis://redis:6379/0"


def test_sensing_redis_url_default_is_localhost(monkeypatch):
    """Without the env var, sensing_redis_url should default to localhost."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["sensing_redis_url"] == "redis://127.0.0.1:6379/0"


# ---------------------------------------------------------------------------
# Fix A3: IIC_SMTP_ENABLED + IIC_SMTP_TO_ADDRS env overrides
# ---------------------------------------------------------------------------

_SMTP_VARS = ["IIC_SMTP_ENABLED", "IIC_SMTP_TO_ADDRS"]


def _reload_smtp(monkeypatch, **overrides):
    """Reload with smtp-related env vars cleared then overridden."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key in _SMTP_VARS:
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


def test_smtp_enabled_true_coercion(monkeypatch):
    """IIC_SMTP_ENABLED=true must land as bool True in config['smtp']['enabled']."""
    dc = _reload_smtp(monkeypatch, IIC_SMTP_ENABLED="true")
    assert dc.DEFAULT_CONFIG["smtp"]["enabled"] is True


def test_smtp_enabled_false_coercion(monkeypatch):
    """IIC_SMTP_ENABLED=false must land as bool False."""
    dc = _reload_smtp(monkeypatch, IIC_SMTP_ENABLED="false")
    assert dc.DEFAULT_CONFIG["smtp"]["enabled"] is False


def test_smtp_to_addrs_comma_split(monkeypatch):
    """IIC_SMTP_TO_ADDRS must be split on commas and blanks stripped."""
    dc = _reload_smtp(
        monkeypatch,
        IIC_SMTP_ENABLED="true",
        IIC_SMTP_TO_ADDRS="alice@example.com, bob@example.com , carol@example.com",
    )
    assert dc.DEFAULT_CONFIG["smtp"]["enabled"] is True
    assert dc.DEFAULT_CONFIG["smtp"]["to_addrs"] == [
        "alice@example.com", "bob@example.com", "carol@example.com"
    ]


# ---------------------------------------------------------------------------
# Fix A4: IIC_LLM_FALLBACK_MODE + IIC_LLM_FALLBACK_DAILY_BUDGET overrides
# ---------------------------------------------------------------------------

_FALLBACK_VARS = [
    "IIC_LLM_FALLBACK_MODE",
    "IIC_LLM_FALLBACK_DAILY_BUDGET",
    "IIC_TRIAGE_LLM_FALLBACK_MODE",
    "IIC_ALERT_GATE_LLM_FALLBACK_MODE",
]


def _reload_fallback(monkeypatch, **overrides):
    """Reload with fallback-related env vars cleared then overridden."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key in _FALLBACK_VARS:
        monkeypatch.delenv(key, raising=False)
    # Also clear per-role vars that _apply_nested_env_overrides reads
    for key in ["IIC_TRIAGE_LLM_PROVIDER", "IIC_TRIAGE_LLM_MODEL",
                "IIC_ALERT_GATE_LLM_PROVIDER", "IIC_ALERT_GATE_LLM_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


def test_llm_fallback_mode_applies_to_both_roles(monkeypatch):
    """IIC_LLM_FALLBACK_MODE=api must set fallback='api' on both llm_roles."""
    dc = _reload_fallback(monkeypatch, IIC_LLM_FALLBACK_MODE="api")
    roles = dc.DEFAULT_CONFIG["llm_roles"]
    assert roles["triage_salience"]["fallback"] == "api"
    assert roles["alert_gate"]["fallback"] == "api"


def test_llm_fallback_mode_none_keeps_default(monkeypatch):
    """IIC_LLM_FALLBACK_MODE=none preserves committed 'none' default."""
    dc = _reload_fallback(monkeypatch, IIC_LLM_FALLBACK_MODE="none")
    roles = dc.DEFAULT_CONFIG["llm_roles"]
    assert roles["triage_salience"]["fallback"] == "none"
    assert roles["alert_gate"]["fallback"] == "none"


def test_llm_fallback_daily_budget_applies_to_both_roles(monkeypatch):
    """IIC_LLM_FALLBACK_DAILY_BUDGET must land as float on both roles."""
    dc = _reload_fallback(monkeypatch, IIC_LLM_FALLBACK_DAILY_BUDGET="200")
    roles = dc.DEFAULT_CONFIG["llm_roles"]
    assert roles["triage_salience"]["fallback_daily_budget"] == 200.0
    assert roles["alert_gate"]["fallback_daily_budget"] == 200.0


def test_per_role_fallback_mode_override(monkeypatch):
    """Per-role IIC_TRIAGE_LLM_FALLBACK_MODE overrides the global setting for that role only."""
    dc = _reload_fallback(
        monkeypatch,
        IIC_LLM_FALLBACK_MODE="api",
        IIC_TRIAGE_LLM_FALLBACK_MODE="none",
    )
    roles = dc.DEFAULT_CONFIG["llm_roles"]
    # triage overridden back to none; alert_gate still gets global 'api'
    assert roles["triage_salience"]["fallback"] == "none"
    assert roles["alert_gate"]["fallback"] == "api"
