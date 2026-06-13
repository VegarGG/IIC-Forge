"""Tests for cli.utils.ensure_api_key, focusing on optional-key providers."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_ensure_api_key_local_no_prompt_when_unset(monkeypatch):
    """ensure_api_key('local') must return None WITHOUT prompting when key unset."""
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

    # Monkeypatch questionary.password so that calling it raises — this
    # proves ensure_api_key does NOT reach the interactive prompt branch.
    import questionary

    def _raise_if_called(*args, **kwargs):
        raise AssertionError(
            "ensure_api_key must not prompt for optional-key providers"
        )

    monkeypatch.setattr(questionary, "password", _raise_if_called)

    from cli.utils import ensure_api_key

    result = ensure_api_key("local")
    assert result is None


@pytest.mark.unit
def test_ensure_api_key_local_returns_value_when_set(monkeypatch):
    """ensure_api_key('local') returns the key value when LOCAL_LLM_API_KEY is set."""
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-lan-test-key")

    from cli.utils import ensure_api_key

    result = ensure_api_key("local")
    assert result == "sk-lan-test-key"
