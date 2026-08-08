"""Tests for Anthropic effort-parameter gating (#831).

Haiku 4.5 rejects the ``effort`` parameter with a 400. Catalogued Opus and
Sonnet models accept it. Unknown model IDs are deliberately absent from this
suite and remain excluded until they are added to the shared model catalog.
"""

import pytest

from tradingagents.llm_clients import anthropic_client as mod
from tradingagents.llm_clients.model_catalog import get_known_models


def _capture_kwargs(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        mod, "NormalizedChatAnthropic",
        lambda **kwargs: captured.setdefault("kwargs", kwargs),
    )
    return captured


@pytest.mark.unit
class TestEffortGate:
    def test_effort_allowlist_contains_only_catalogued_models(self):
        known = set(get_known_models()["anthropic"])
        assert mod._EFFORT_MODELS <= known

    def test_haiku_does_not_receive_effort(self, monkeypatch):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-haiku-4-5", effort="medium", api_key="x"
        ).get_llm()
        assert "effort" not in captured["kwargs"]

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7",
            "claude-sonnet-4-5", "claude-sonnet-4-6",
        ],
    )
    def test_current_opus_and_sonnet_receive_effort(self, monkeypatch, model):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="high", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "high"

    def test_other_kwargs_still_forwarded_when_effort_skipped(self, monkeypatch):
        """Skipping effort must not break other passthrough kwargs."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-haiku-4-5",
            effort="medium",
            api_key="placeholder",
            max_tokens=1024,
            timeout=30,
        ).get_llm()
        assert captured["kwargs"]["api_key"] == "placeholder"
        assert captured["kwargs"]["max_tokens"] == 1024
        assert captured["kwargs"]["timeout"] == 30
        assert "effort" not in captured["kwargs"]
