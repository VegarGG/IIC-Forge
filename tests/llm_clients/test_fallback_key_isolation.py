"""Isolated-key behaviour for the early-testing classification fallback.

Covers: explicit-api_key precedence in OpenAIClient.get_llm; create_role_llm
forwarding an explicit key; resolve_role_llm_global injecting
IIC_LLM_FALLBACK_API_KEY and refusing when it is absent (never borrowing the
worker's DEEPSEEK_API_KEY); the startup guardrail helper.
"""

from __future__ import annotations

import logging

import pytest

from tradingagents.llm_clients.factory import create_llm_client, create_role_llm


@pytest.mark.unit
def test_explicit_api_key_overrides_env_and_skips_raise(monkeypatch):
    # DEEPSEEK_API_KEY absent: without an explicit key get_llm() would raise.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    llm = create_llm_client(
        provider="deepseek", model="deepseek-chat", api_key="sk-explicit"
    ).get_llm()
    assert llm.openai_api_key.get_secret_value() == "sk-explicit"


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


@pytest.mark.unit
def test_create_role_llm_forwards_explicit_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = create_role_llm(
        "triage_salience", _cfg_global_deepseek(), api_key="sk-role")
    assert client.get_llm().openai_api_key.get_secret_value() == "sk-role"


@pytest.mark.unit
def test_resolve_role_llm_global_injects_fallback_key(monkeypatch):
    from tradingagents.llm_clients.availability import resolve_role_llm_global
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("IIC_LLM_FALLBACK_API_KEY", "sk-fallback")
    client = resolve_role_llm_global("triage_salience", _cfg_global_deepseek())
    assert client.get_llm().openai_api_key.get_secret_value() == "sk-fallback"


@pytest.mark.unit
def test_resolve_role_llm_global_refuses_without_key(monkeypatch):
    from tradingagents.llm_clients.availability import (
        LocalEndpointUnavailable, resolve_role_llm_global,
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "worker-key")
    monkeypatch.delenv("IIC_LLM_FALLBACK_API_KEY", raising=False)
    with pytest.raises(LocalEndpointUnavailable, match="IIC_LLM_FALLBACK_API_KEY"):
        resolve_role_llm_global("triage_salience", _cfg_global_deepseek())


@pytest.mark.unit
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


_LOG = logging.getLogger("test.fallback.guardrail")


@pytest.mark.unit
def test_guardrail_warns_when_api_and_budget_zero(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("triage_salience", "api", 0,
                                   fallback_key_present=True, log=_LOG)
    assert "triage_salience" in caplog.text
    assert "budget" in caplog.text.lower()


@pytest.mark.unit
def test_guardrail_warns_when_api_and_key_missing(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("alert_gate", "api", 500,
                                   fallback_key_present=False, log=_LOG)
    assert "alert_gate" in caplog.text
    assert "IIC_LLM_FALLBACK_API_KEY" in caplog.text


@pytest.mark.unit
def test_guardrail_silent_when_satisfiable(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("triage_salience", "api", 500,
                                   fallback_key_present=True, log=_LOG)
    assert caplog.text == ""


@pytest.mark.unit
def test_guardrail_silent_when_fallback_none(caplog):
    from tradingagents.llm_clients.availability import warn_if_fallback_unsatisfiable
    caplog.set_level(logging.WARNING)
    warn_if_fallback_unsatisfiable("triage_salience", "none", 0,
                                   fallback_key_present=False, log=_LOG)
    assert caplog.text == ""
