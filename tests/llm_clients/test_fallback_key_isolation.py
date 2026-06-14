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
