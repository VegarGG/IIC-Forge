"""Capability-gated json_schema response_format binding tests.

Covers:
  - evaluate_alert_candidate: json_schema response_format reaches the wire body
    when model_id resolves to a json_schema-capable model (qwen3.6-27b-…);
    NO response_format key when model_id resolves to a non-capable model
    (deepseek-v4-flash).
  - maybe_bind_salience_schema: binds and sends json_schema for capable models;
    passes through the original llm for non-capable models.
  - triage._main: after the create_role_llm call, the llm is bound when the
    resolved model supports json_schema (via maybe_bind_salience_schema).

Uses the StubOpenAIServer fixture (stub_openai_server from conftest.py) for
real wire-level assertions — no mocking of the LLM layer.
"""

from __future__ import annotations

import copy
import pytest


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_BASE_CFG_LOCAL = {
    "llm_provider": "deepseek",
    "quick_think_llm": "deepseek-v4-flash",
    "backend_url": None,
    "llm_roles": {
        "triage_salience": {
            "provider": "local",
            "model": "qwen3.6-27b-instruct-q4_k_m",
            "base_url": None,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
    },
}

def _make_local_cfg(base_url: str) -> dict:
    cfg = copy.deepcopy(_BASE_CFG_LOCAL)
    cfg["llm_roles"]["triage_salience"]["base_url"] = base_url
    return cfg


# ---------------------------------------------------------------------------
# Evaluator: wire assertion via stub server
# ---------------------------------------------------------------------------


class TestEvaluatorJsonSchemaBinding:
    """evaluate_alert_candidate attaches response_format on capable models."""

    @pytest.mark.unit
    def test_capable_model_sends_json_schema_response_format(
        self, stub_openai_server, monkeypatch
    ):
        """json_schema response_format is in the wire body for qwen3.6-27b (capable)."""
        monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

        from tradingagents.llm_clients.factory import create_role_llm
        from tradingagents.orchestrator.alert_evaluator import evaluate_alert_candidate

        cfg = _make_local_cfg(stub_openai_server.url + "/v1")
        client = create_role_llm("triage_salience", cfg)
        llm = client.get_llm()

        evaluate_alert_candidate(
            llm=llm,
            event_text="NVDA raises guidance after earnings.",
            tickers=["NVDA"],
            min_score=0.80,
            model_id="qwen3.6-27b-instruct-q4_k_m",
        )

        body = stub_openai_server.last_request_json
        assert body is not None, "Stub server did not receive any request"
        assert "response_format" in body, (
            f"Expected response_format in body for capable model; "
            f"got keys: {list(body.keys())}"
        )
        assert body["response_format"]["type"] == "json_schema", (
            f"Expected type=json_schema; got {body['response_format']!r}"
        )

    @pytest.mark.unit
    def test_incapable_model_sends_no_response_format(
        self, stub_openai_server, monkeypatch
    ):
        """No response_format key in wire body for deepseek-v4-flash (incapable)."""
        monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

        from tradingagents.llm_clients.factory import create_role_llm
        from tradingagents.orchestrator.alert_evaluator import evaluate_alert_candidate

        cfg = _make_local_cfg(stub_openai_server.url + "/v1")
        client = create_role_llm("triage_salience", cfg)
        llm = client.get_llm()

        evaluate_alert_candidate(
            llm=llm,
            event_text="generic market chatter",
            tickers=["AAPL"],
            min_score=0.80,
            model_id="deepseek-v4-flash",
        )

        body = stub_openai_server.last_request_json
        assert body is not None, "Stub server did not receive any request"
        assert "response_format" not in body, (
            f"Expected NO response_format for incapable model; "
            f"got keys: {list(body.keys())}"
        )


@pytest.mark.unit
def test_evaluator_bare_llm_no_model_name_no_bind(stub_openai_server, monkeypatch):
    """BareLLM (no model_name, explicit model_id=None): no bind, no response_format."""
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

    from tradingagents.orchestrator.alert_evaluator import evaluate_alert_candidate

    # A BareLLM that POST to the stub server so we can inspect the body,
    # but has no model_name attribute — simulate using the stub server client
    # but override the model to avoid the llm.model_name fallback.
    from tradingagents.llm_clients.factory import create_role_llm
    import copy

    cfg_local = {
        "llm_provider": "deepseek",
        "quick_think_llm": "deepseek-v4-flash",
        "backend_url": None,
        "llm_roles": {
            "triage_salience": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": stub_openai_server.url + "/v1",
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            }
        },
    }
    client = create_role_llm("triage_salience", cfg_local)
    llm = client.get_llm()

    # When both model_id=None AND llm.model_name exists, _resolve_model_id
    # returns llm.model_name (qwen3.6-27b…). That model IS capable so bind
    # WILL happen. To test the None path, use a wrapper that hides model_name.
    class NoModelNameWrapper:
        """Wrapper that forwards invoke but hides model_name (mimics BareLLM)."""
        def __init__(self, inner):
            self._inner = inner

        def invoke(self, prompt, **kwargs):
            return self._inner.invoke(prompt, **kwargs)

        def bind(self, **kwargs):
            # Proxy bind so the wrapped llm can still be bound if needed.
            bound = self._inner.bind(**kwargs)

            class _BoundWrapper:
                def __init__(self, b):
                    self._b = b

                def invoke(self, prompt, **kwargs2):
                    return self._b.invoke(prompt, **kwargs2)

            return _BoundWrapper(bound)

    wrapped = NoModelNameWrapper(llm)
    result = evaluate_alert_candidate(
        llm=wrapped,
        event_text="generic market chatter",
        tickers=["AAPL"],
        min_score=0.80,
        model_id=None,  # explicit None AND no model_name → resolved=None → no bind
    )

    body = stub_openai_server.last_request_json
    assert body is not None
    assert "response_format" not in body, (
        f"Expected NO response_format when resolved_model_id=None; "
        f"got keys: {list(body.keys())}"
    )
    assert result.model_id is None


# ---------------------------------------------------------------------------
# maybe_bind_salience_schema: unit tests via stub server
# ---------------------------------------------------------------------------

class TestMaybeBindSalienceSchema:
    """maybe_bind_salience_schema wires json_schema when capable; passthrough otherwise."""

    @pytest.mark.unit
    def test_capable_model_binds_response_format_wire(
        self, stub_openai_server, monkeypatch
    ):
        """Bound llm sends json_schema response_format on the wire."""
        monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

        from tradingagents.llm_clients.factory import create_role_llm
        from tradingagents.sensing.salience import maybe_bind_salience_schema

        cfg = _make_local_cfg(stub_openai_server.url + "/v1")
        client = create_role_llm("triage_salience", cfg)
        llm = client.get_llm()

        bound = maybe_bind_salience_schema(llm, "qwen3.6-27b-instruct-q4_k_m")
        bound.invoke("classify this")

        body = stub_openai_server.last_request_json
        assert body is not None
        assert "response_format" in body, (
            f"Expected response_format in body after bind; got {list(body.keys())}"
        )
        assert body["response_format"]["type"] == "json_schema"

    @pytest.mark.unit
    def test_incapable_model_returns_original_llm_no_format(
        self, stub_openai_server, monkeypatch
    ):
        """Non-capable model: maybe_bind_salience_schema returns original llm unbound."""
        monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

        from tradingagents.llm_clients.factory import create_role_llm
        from tradingagents.sensing.salience import maybe_bind_salience_schema

        cfg = _make_local_cfg(stub_openai_server.url + "/v1")
        client = create_role_llm("triage_salience", cfg)
        llm = client.get_llm()

        result_llm = maybe_bind_salience_schema(llm, "deepseek-v4-flash")
        # Should be the same object (no bind)
        assert result_llm is llm, (
            "maybe_bind_salience_schema should return original llm for incapable model"
        )
        result_llm.invoke("classify this")

        body = stub_openai_server.last_request_json
        assert body is not None
        assert "response_format" not in body, (
            f"Expected NO response_format for incapable model; got {list(body.keys())}"
        )

    @pytest.mark.unit
    def test_empty_model_id_returns_original_llm(
        self, stub_openai_server, monkeypatch
    ):
        """Empty/falsy model_id: maybe_bind_salience_schema returns original llm."""
        monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

        from tradingagents.llm_clients.factory import create_role_llm
        from tradingagents.sensing.salience import maybe_bind_salience_schema

        cfg = _make_local_cfg(stub_openai_server.url + "/v1")
        client = create_role_llm("triage_salience", cfg)
        llm = client.get_llm()

        result_llm = maybe_bind_salience_schema(llm, "")
        assert result_llm is llm


# ---------------------------------------------------------------------------
# Triage _main: bind happens when model is capable
# ---------------------------------------------------------------------------

class TestTriageMainBindsWhenCapable:
    """_main wires maybe_bind_salience_schema; bound llm sends json_schema on wire."""

    @pytest.mark.unit
    def test_main_binds_llm_for_capable_model(self, stub_openai_server, monkeypatch):
        """After _main's create_role_llm call, the llm is bound when model is capable.

        Strategy: patch maybe_bind_salience_schema to record whether it was
        called with the right model_id, then raise _Bail to stop _main early.
        This directly tests that _main calls the helper after create_role_llm.
        """
        import tradingagents.llm_clients.factory as factory_mod
        import tradingagents.sensing.salience as salience_mod
        from tradingagents.default_config import DEFAULT_CONFIG

        class _Bail(Exception):
            pass

        captured = {}

        original_maybe_bind = salience_mod.maybe_bind_salience_schema

        def fake_maybe_bind(llm, model_id):
            captured["llm"] = llm
            captured["model_id"] = model_id
            raise _Bail("bail after recording bind call")

        monkeypatch.setattr(salience_mod, "maybe_bind_salience_schema", fake_maybe_bind)

        # Stub out the rest of _main so we reach the bind call.
        import tradingagents.sensing.redis_client as redis_mod
        monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())

        import tradingagents.persistence.db as db_mod
        monkeypatch.setattr(db_mod, "connect", lambda path: object())

        import tradingagents.sensing.embeddings as emb_mod
        class _FakeEmbedder:
            pass
        monkeypatch.setattr(
            emb_mod, "SentenceTransformerEmbedder", lambda model: _FakeEmbedder()
        )

        monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", "/tmp/test_iic.db")
        monkeypatch.setitem(DEFAULT_CONFIG, "sensing_redis_url", "redis://localhost:6379/0")

        # Patch create_role_llm to return a fake client with a known capable model.
        class _FakeLLM:
            def invoke(self, prompt):
                from types import SimpleNamespace
                return SimpleNamespace(content='{"ok": true}')

        class _FakeClient:
            model = "qwen3.6-27b-instruct-q4_k_m"

            def get_llm(self):
                return _FakeLLM()

        monkeypatch.setattr(
            factory_mod, "create_role_llm", lambda role, cfg: _FakeClient()
        )

        from tradingagents.sensing.triage import _main
        with pytest.raises(_Bail):
            _main()

        assert "model_id" in captured, "_main did not call maybe_bind_salience_schema"
        assert captured["model_id"] == "qwen3.6-27b-instruct-q4_k_m", (
            f"Expected model_id=qwen3.6-27b-instruct-q4_k_m; "
            f"got {captured['model_id']!r}"
        )

    @pytest.mark.unit
    def test_main_bind_deleted_causes_failure(self, monkeypatch):
        """If the bind line is removed from _main, maybe_bind_salience_schema is
        not called, and this test fails — proving the deletion would break the gate.

        Implementation: we verify that the source of _main contains the actual
        function *call* (with opening paren), not merely the import/reference.
        Matching just the bare name would pass even when the import is present
        but the call line is deleted — the paren anchor prevents that false-green.
        """
        import inspect
        from tradingagents.sensing import triage
        src = inspect.getsource(triage._main)
        assert "maybe_bind_salience_schema(" in src, (
            "_main must call maybe_bind_salience_schema(...) to attach json_schema "
            "response_format — deleting this line breaks the D4 L1 exit gate."
        )


# ---------------------------------------------------------------------------
# Fix 1: unknown model ids must NOT receive json_schema binding (fail-closed)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_unknown_model_id_no_json_schema_capability():
    """get_capabilities for a totally unknown model id must return supports_json_schema=False.

    json_schema binding is opt-in via an explicit capability row.  An unrowed
    model (qwen-max, glm-4.x, openrouter slugs, etc.) must never get
    response_format=json_schema attached — that would hard-400 from unknown
    providers and crash-loop triage or brick the evaluator.
    """
    from tradingagents.llm_clients.capabilities import get_capabilities
    caps = get_capabilities("totally-unknown-model-xyz")
    assert caps.supports_json_schema is False, (
        "Unknown model ids must fail-closed: supports_json_schema must be False "
        "for 'totally-unknown-model-xyz' (got True, meaning _DEFAULT is still True)"
    )


@pytest.mark.unit
def test_unknown_model_id_no_bind_wire(stub_openai_server, monkeypatch):
    """maybe_bind_salience_schema must NOT attach response_format for unknown model ids.

    Wire-level assertion via the stub server: the request body must have no
    response_format key when the model_id is totally unknown (falls through to
    _DEFAULT which must have supports_json_schema=False after Fix 1).
    """
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

    from tradingagents.llm_clients.factory import create_role_llm
    from tradingagents.sensing.salience import maybe_bind_salience_schema

    cfg = _make_local_cfg(stub_openai_server.url + "/v1")
    client = create_role_llm("triage_salience", cfg)
    llm = client.get_llm()

    result_llm = maybe_bind_salience_schema(llm, "totally-unknown-model-xyz")
    result_llm.invoke("classify this")

    body = stub_openai_server.last_request_json
    assert body is not None, "Stub server did not receive any request"
    assert "response_format" not in body, (
        f"Unknown model 'totally-unknown-model-xyz' must NOT receive "
        f"response_format=json_schema (fail-closed); got keys: {list(body.keys())}"
    )
