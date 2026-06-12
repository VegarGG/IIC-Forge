import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_alert_gate_records_llm_call_on_success(tmp_path):
    from tradingagents.orchestrator.alert_evaluator import record_alert_gate_llm_call

    conn = connect(str(tmp_path / "iic.db"))
    store.insert_event(
        conn,
        event_id="ev1",
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        salience=0.9,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="llm",
    )
    record_alert_gate_llm_call(
        conn,
        event_id="ev1",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        latency_ms=111,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
    )
    row = store.fetch_llm_calls(conn, role="alert_gate")[0]
    assert row["service_name"] == "promoter"
    assert row["linked_type"] == "event"
    assert row["linked_id"] == "ev1"
    assert row["status"] == "success"


@pytest.mark.unit
def test_light_summary_records_llm_call(tmp_path):
    from tradingagents.secretary.service import record_light_summary_llm_call

    conn = connect(str(tmp_path / "iic.db"))
    record_light_summary_llm_call(
        conn,
        brief_id="brief1",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        latency_ms=88,
        fallback_mode="none",
        fallback_used=False,
    )
    row = store.fetch_llm_calls(conn, role="light_alert_summary")[0]
    assert row["linked_type"] == "brief"
    assert row["linked_id"] == "brief1"
    assert row["usd_estimate"] == 0.0
