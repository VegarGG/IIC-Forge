"""Test that promoter.main builds its LLM via create_role_llm("alert_gate", cfg).

Patch target: tradingagents.llm_clients.factory.create_role_llm
(promoter.main imports create_role_llm from the factory module at call time,
so patching the factory module is sufficient to intercept the call).
"""

import pytest


class _Bail(Exception):
    """Sentinel: raised by the patched create_role_llm to exit main early."""


@pytest.mark.unit
def test_promoter_main_calls_create_role_llm_with_alert_gate(monkeypatch, tmp_path):
    """promoter.main must call create_role_llm("alert_gate", cfg) when building its LLM."""
    import tradingagents.llm_clients.factory as factory_mod
    from tradingagents.default_config import DEFAULT_CONFIG

    captured = {}

    def fake_create_role_llm(role, config):
        captured["role"] = role
        captured["config"] = config
        raise _Bail("bail after recording")

    # Patch at the factory module so any import-time or call-time binding sees it.
    monkeypatch.setattr(factory_mod, "create_role_llm", fake_create_role_llm)

    # Also stub create_llm_client so the old code path raises _Bail too (prevents hang
    # in the red phase when create_role_llm has not yet been wired in).
    def fake_create_llm_client(*args, **kwargs):
        raise _Bail("create_llm_client called instead of create_role_llm — test will fail")

    monkeypatch.setattr(factory_mod, "create_llm_client", fake_create_llm_client)

    # promoter.py imports connect at module level via
    # `from tradingagents.persistence.db import connect`, so we must patch
    # the already-bound name in the promoter module's namespace.
    import tradingagents.orchestrator.promoter as promoter_mod
    monkeypatch.setattr(promoter_mod, "connect", lambda path: object())

    # Enable the gate so the LLM-client branch is reached.
    # Pass config so that iic_db_path is valid AND gate is enabled.
    test_cfg = {
        "iic_db_path": str(tmp_path / "iic.db"),
        "alert_approval_gate_enabled": True,
    }

    from tradingagents.orchestrator.promoter import main
    with pytest.raises(_Bail):
        main(config=test_cfg)

    assert captured.get("role") == "alert_gate", (
        f"Expected create_role_llm called with 'alert_gate', got {captured.get('role')!r}"
    )
    # Config object must be a dict merging DEFAULT_CONFIG with test overrides.
    assert isinstance(captured.get("config"), dict), "config argument must be a dict"
    assert captured["config"].get("llm_provider") == DEFAULT_CONFIG["llm_provider"], (
        "config passed to create_role_llm must have the correct llm_provider"
    )
    assert captured["config"].get("alert_approval_gate_enabled") is True, (
        "config passed to create_role_llm must reflect the merged config"
    )
