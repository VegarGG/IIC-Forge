import pytest

from tradingagents.persistence.db import connect


@pytest.mark.unit
def test_record_llm_success_defaults_local_cost_to_zero(tmp_path):
    from tradingagents.llm_clients.ledger import record_llm_success
    from tradingagents.persistence import store

    conn = connect(str(tmp_path / "iic.db"))
    call_id = record_llm_success(
        conn,
        role="alert_gate",
        service_name="promoter",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        latency_ms=99,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        token_usage={"prompt_tokens": 12, "completion_tokens": 3},
    )
    row = store.fetch_llm_calls(conn)[0]
    assert row["call_id"] == call_id
    assert row["status"] == "success"
    assert row["usd_estimate"] == 0.0
    assert row["in_tokens"] == 12
    assert row["out_tokens"] == 3


@pytest.mark.unit
def test_record_llm_success_nonlocal_provider_cost_is_unknown(tmp_path):
    from tradingagents.llm_clients.ledger import record_llm_success
    from tradingagents.persistence import store

    conn = connect(str(tmp_path / "iic.db"))
    record_llm_success(
        conn,
        role="alert_gate",
        service_name="promoter",
        provider="deepseek",
        model_id="deepseek-chat",
        base_url="https://api.deepseek.com",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        latency_ms=99,
        parse_ok=True,
        fallback_mode="api",
        fallback_used=True,
        token_usage={"prompt_tokens": 12, "completion_tokens": 3},
    )
    row = store.fetch_llm_calls(conn)[0]
    assert row["usd_estimate"] is None
    assert row["fallback_used"] == 1


@pytest.mark.unit
def test_record_llm_success_explicit_usd_overrides_local_zero(tmp_path):
    from tradingagents.llm_clients.ledger import record_llm_success
    from tradingagents.persistence import store

    conn = connect(str(tmp_path / "iic.db"))
    record_llm_success(
        conn,
        role="alert_gate",
        service_name="promoter",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        latency_ms=99,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        usd_estimate=0.25,
    )
    row = store.fetch_llm_calls(conn)[0]
    assert row["usd_estimate"] == 0.25


@pytest.mark.unit
def test_record_llm_error_truncates_message_and_classifies_status(tmp_path):
    from tradingagents.llm_clients.ledger import record_llm_error
    from tradingagents.persistence import store

    conn = connect(str(tmp_path / "iic.db"))
    record_llm_error(
        conn,
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev2",
        status="parse_error",
        latency_ms=250,
        parse_ok=False,
        fallback_mode="none",
        fallback_used=False,
        exc=ValueError("x" * 2000),
    )
    row = store.fetch_llm_calls(conn)[0]
    assert row["status"] == "parse_error"
    assert row["error_class"] == "ValueError"
    assert len(row["error_message"]) == 1000
