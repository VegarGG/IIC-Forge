import json

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
def test_record_llm_call_round_trip(conn):
    call_id = store.insert_llm_call(
        conn,
        created_ts="2026-06-12T10:00:00+00:00",
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        status="success",
        latency_ms=123,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=10,
        out_tokens=5,
        cache_hit_tokens=0,
        cache_miss_tokens=10,
        usd_estimate=0.0,
        error_class=None,
        error_message=None,
    )
    rows = store.fetch_llm_calls(conn, role="triage_salience")
    assert rows == [{
        "call_id": call_id,
        "created_ts": "2026-06-12T10:00:00+00:00",
        "role": "triage_salience",
        "service_name": "triage",
        "provider": "local",
        "model_id": "qwen3.6-27b-instruct-q4_k_m",
        "base_url": "http://host.docker.internal:8080/v1",
        "request_kind": "structured",
        "linked_type": "event",
        "linked_id": "ev1",
        "status": "success",
        "latency_ms": 123,
        "parse_ok": 1,
        "fallback_mode": "none",
        "fallback_used": 0,
        "in_tokens": 10,
        "out_tokens": 5,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 10,
        "usd_estimate": 0.0,
        "error_class": None,
        "error_message": None,
    }]


@pytest.mark.unit
def test_source_health_upsert_success_and_failure(conn):
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:00:00+00:00",
        last_success_ts="2026-06-12T10:00:01+00:00",
        last_event_ts="2026-06-12T10:00:02+00:00",
        cursor="20260612T100000Z",
        cursor_updated_ts="2026-06-12T10:00:03+00:00",
        events_emitted_last_poll=2,
        diagnostics={"quota": "ok"},
    )
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["events_emitted_total"] == 2
    assert row["events_emitted_last_poll"] == 2
    assert row["consecutive_failures"] == 0
    assert json.loads(row["diagnostics"]) == {"quota": "ok"}

    store.upsert_source_health_failure(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:05:00+00:00",
        error="HTTP 500",
        diagnostics={"url": "gdelt"},
    )
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["events_emitted_total"] == 2
    assert row["consecutive_failures"] == 1
    assert row["last_error"] == "HTTP 500"


@pytest.mark.unit
def test_deferred_salience_retry_lifecycle(conn):
    retry_id = store.insert_deferred_salience_retry(
        conn,
        event_id="ev-deferred",
        source="rss",
        raw_path="/data/events/staging/a.json",
        payload_hash="hash1",
        payload_json='{"source":"rss","text":"earnings shock"}',
        reason="llm_error",
        next_attempt_ts="2026-06-12T10:01:00+00:00",
    )
    due = store.claim_due_deferred_salience_retries(
        conn,
        now_ts="2026-06-12T10:02:00+00:00",
        limit=5,
    )
    assert [row["retry_id"] for row in due] == [retry_id]
    running = store.fetch_deferred_salience_retries(conn, state="running")
    assert running[0]["attempt_count"] == 1

    store.reschedule_deferred_salience_retry(
        conn,
        retry_id=retry_id,
        reason="parse_error",
        next_attempt_ts="2026-06-12T10:06:00+00:00",
    )
    pending = store.fetch_deferred_salience_retries(conn, state="pending")
    assert pending[0]["last_error"] == "parse_error"
    assert pending[0]["next_attempt_ts"] == "2026-06-12T10:06:00+00:00"

    store.mark_deferred_salience_retry_done(conn, retry_id=retry_id)
    done = store.fetch_deferred_salience_retries(conn, state="done")
    assert done[0]["retry_id"] == retry_id


@pytest.mark.unit
def test_delivery_chain_columns_round_trip(conn):
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert_light",
        scope='["NVDA"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    primary = store.insert_delivery(
        conn,
        brief_id="b1",
        channel="telegram",
        status="failed",
        sent_ts=None,
        channel_ref=None,
        skip_reason=None,
        delivery_group_id="grp1",
        attempt_rank=1,
        fallback_of=None,
        is_fallback=False,
        failure_reason="network",
    )
    fallback = store.insert_delivery(
        conn,
        brief_id="b1",
        channel="email",
        status="sent",
        sent_ts="2026-06-12T10:00:05+00:00",
        channel_ref="msg1",
        skip_reason=None,
        delivery_group_id="grp1",
        attempt_rank=2,
        fallback_of=primary,
        is_fallback=True,
        failure_reason=None,
    )
    chains = store.fetch_delivery_groups(conn)
    assert chains["grp1"][0]["delivery_id"] == primary
    assert chains["grp1"][1]["delivery_id"] == fallback
    assert chains["grp1"][1]["fallback_of"] == primary
