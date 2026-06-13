"""Test that triage._main builds its LLM via create_role_llm("triage_salience", C).

Patch target: tradingagents.llm_clients.factory.create_role_llm
(triage._main imports create_role_llm from the factory module at call time,
so patching the factory module is sufficient to intercept the call).
"""

import pytest


class _Bail(Exception):
    """Sentinel: raised by the patched create_role_llm to exit _main early."""


@pytest.mark.unit
def test_triage_main_calls_create_role_llm_with_triage_salience(monkeypatch, tmp_path):
    """_main must call create_role_llm("triage_salience", C) when building its LLM."""
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

    # triage._main imports connect/make_redis/SentenceTransformerEmbedder inside the
    # function, so we can patch at the source modules.
    import tradingagents.sensing.redis_client as redis_mod
    monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())

    import tradingagents.persistence.db as db_mod
    monkeypatch.setattr(db_mod, "connect", lambda path: object())

    import tradingagents.sensing.embeddings as emb_mod
    class _FakeEmbedder:
        pass
    monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder", lambda model: _FakeEmbedder())

    # Override iic_db_path in DEFAULT_CONFIG so no real FS access is needed.
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", str(tmp_path / "iic.db"))
    monkeypatch.setitem(DEFAULT_CONFIG, "sensing_redis_url", "redis://localhost:6379/0")

    from tradingagents.sensing.triage import _main
    with pytest.raises(_Bail):
        _main()

    assert captured.get("role") == "triage_salience", (
        f"Expected create_role_llm called with 'triage_salience', got {captured.get('role')!r}"
    )
    # Config object must be the DEFAULT_CONFIG dict.
    assert isinstance(captured.get("config"), dict), "config argument must be a dict"
    assert captured["config"].get("llm_provider") == DEFAULT_CONFIG["llm_provider"], (
        "config passed to create_role_llm must have the correct llm_provider"
    )
