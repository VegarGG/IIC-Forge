"""Stub-server contract tests for the local-provider request shape.

Task 6 — verifies:
  1. The local role client sends ``enable_thinking=False`` in the request body
     (via extra_body merged top-level by the openai SDK).
  2. A ``json_schema`` response_format is forwarded correctly when the call
     site passes it (as later call sites in Tasks 9-10 will do).

No GPU / no real server required: all traffic is intercepted by the
StubOpenAIServer fixture defined in conftest.py.
"""

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CFG = {
    "llm_provider": "deepseek",
    "quick_think_llm": "deepseek-v4-flash",
    "backend_url": None,
    "llm_roles": {
        "triage_salience": {
            "provider": "local",
            "model": "qwen3.6-27b-instruct-q4_k_m",
            # base_url filled in per-test from the stub server
            "base_url": None,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
    },
}


def _make_cfg(base_url: str) -> dict:
    """Return a deep-copied config with the stub server's base_url filled in."""
    import copy
    cfg = copy.deepcopy(_BASE_CFG)
    cfg["llm_roles"]["triage_salience"]["base_url"] = base_url
    return cfg


# ---------------------------------------------------------------------------
# Test 1: enable_thinking reaches the wire body
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_request_disables_thinking(
    stub_openai_server, monkeypatch
):
    """The local role must send enable_thinking=False in the request body.

    extra_body is merged top-level by the openai SDK, so the wire body has
    ``body["chat_template_kwargs"]["enable_thinking"] == False``, not nested
    under an ``extra_body`` key.
    """
    # Hermetic: no env pollution from a running local server.
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

    from tradingagents.llm_clients.factory import create_role_llm

    cfg = _make_cfg(stub_openai_server.url + "/v1")
    llm = create_role_llm("triage_salience", cfg).get_llm()
    llm.invoke("classify this")

    body = stub_openai_server.last_request_json
    assert body is not None, "Stub server did not receive any request"
    # extra_body contents are merged top-level into the wire body by the openai SDK.
    assert "chat_template_kwargs" in body, (
        f"Expected chat_template_kwargs at top level of body; got keys: {list(body.keys())}"
    )
    assert body["chat_template_kwargs"]["enable_thinking"] is False


# ---------------------------------------------------------------------------
# Test 2: json_schema response_format reaches the wire body
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_request_sends_json_schema_response_format(
    stub_openai_server, monkeypatch
):
    """When a call site binds a json_schema response_format, it must appear in
    the request body sent to the local model endpoint.

    We use ``.bind(response_format=...)`` rather than passing response_format
    as an invoke kwarg because langchain-openai 1.x routes the ``parse`` code
    path when ``response_format`` appears in the payload, which requires a
    Python type object (Pydantic / dataclass) for structured-output parsing.
    A plain dict passed via invoke() would trigger that code path with an
    unsupported type and raise.  ``.bind()`` pre-sets the kwarg so the
    underlying ChatOpenAI implementation receives it in exactly the same way
    later call sites in the codebase will.
    """
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

    from tradingagents.llm_clients.factory import create_role_llm

    cfg = _make_cfg(stub_openai_server.url + "/v1")
    llm = create_role_llm("triage_salience", cfg).get_llm()

    json_schema_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "salience",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        },
    }
    llm_with_format = llm.bind(response_format=json_schema_format)
    llm_with_format.invoke("classify this")

    body = stub_openai_server.last_request_json
    assert body is not None, "Stub server did not receive any request"
    assert "response_format" in body, (
        f"Expected response_format in body; got keys: {list(body.keys())}"
    )
    assert body["response_format"]["type"] == "json_schema"
