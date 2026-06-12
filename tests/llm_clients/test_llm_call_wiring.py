import json

import fakeredis.aioredis
import pytest
from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# Fix 8: Real Triage.process_one wiring tests
# ---------------------------------------------------------------------------

def _make_llm_response(salience=0.9, ticker="AAPL", conf=0.95):
    """Return a sync callable that produces a valid SalienceResult JSON string."""
    def call(_prompt):
        return json.dumps({
            "salience": salience,
            "matched_tickers": [ticker],
            "mentioned_tickers": [{"ticker": ticker, "confidence": conf}],
            "reason": "unit test",
        })
    return call


def _make_deferred_llm():
    """Return a sync callable that raises, causing a deferred (transport_error) score."""
    def call(_prompt):
        raise ConnectionError("simulated local endpoint down")
    return call


def _make_bad_json_llm():
    """Return a sync callable that returns garbage JSON, causing a parse_error score."""
    def call(_prompt):
        return "not valid json {"
    return call


def _env(text="Apple beats Q3 revenue estimates", source="polygon_news"):
    from tradingagents.sensing.envelope import Envelope
    return Envelope(
        source=source,
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id=f"x:{text[:8]}",
        text=text,
        source_tags={},
        raw_path="data/events/staging/x.json",
    )


@pytest.fixture
def wired_conn(tmp_path):
    """DB connection with AAPL in tickers table."""
    from tradingagents.persistence.store import upsert_ticker
    conn = connect(str(tmp_path / "iic.db"))
    upsert_ticker(conn, ticker="AAPL", exchange="NASDAQ",
                  name="Apple Inc.", aliases=[], active=True)
    return conn


@pytest.mark.unit
async def test_triage_process_one_success_writes_llm_call(wired_conn, tmp_path):
    """A successfully scored event produces a triage_salience success row in llm_calls."""
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(
        conn=wired_conn,
        redis=r,
        embedder=MockEmbedder(),
        llm_call=_make_llm_response(),
        data_dir=str(tmp_path / "data"),
    )
    # Stamp scorer identity (normally done by _main; tests construct Triage directly)
    t._scorer.provider = "local"
    t._scorer.model_id = "test-model"
    t._scorer.base_url = "http://localhost:8080/v1"
    t._scorer.fallback_mode = "none"
    t._scorer.fallback_used = False

    res = await t.process_one(_env())
    assert res.status == "triaged"

    rows = store.fetch_llm_calls(wired_conn, role="triage_salience")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "success"
    assert row["service_name"] == "triage"
    assert row["provider"] == "local"
    assert row["linked_type"] == "event"
    # usd_estimate is 0.0 for local provider
    assert row["usd_estimate"] == 0.0


@pytest.mark.unit
async def test_triage_process_one_deferred_writes_transport_error_row(wired_conn, tmp_path):
    """A transport failure (LLM raises) produces a triage_salience transport_error row."""
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(
        conn=wired_conn,
        redis=r,
        embedder=MockEmbedder(),
        llm_call=_make_deferred_llm(),
        data_dir=str(tmp_path / "data"),
    )
    t._scorer.provider = "local"
    t._scorer.model_id = "test-model"
    t._scorer.base_url = "http://localhost:8080/v1"
    t._scorer.fallback_mode = "api"
    t._scorer.fallback_used = False

    res = await t.process_one(_env(text="Different text so no dedupe"))
    assert res.status == "triaged"  # event is persisted even when deferred

    rows = store.fetch_llm_calls(wired_conn, role="triage_salience")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "transport_error"
    assert row["service_name"] == "triage"


@pytest.mark.unit
async def test_triage_process_one_parse_error_writes_parse_error_row(wired_conn, tmp_path):
    """A parse failure (bad JSON from LLM) produces a triage_salience parse_error row."""
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(
        conn=wired_conn,
        redis=r,
        embedder=MockEmbedder(),
        llm_call=_make_bad_json_llm(),
        data_dir=str(tmp_path / "data"),
    )
    t._scorer.provider = "local"
    t._scorer.model_id = "test-model"
    t._scorer.base_url = None
    t._scorer.fallback_mode = "none"
    t._scorer.fallback_used = False

    res = await t.process_one(_env(text="Yet another unique text for parse error test"))
    assert res.status == "triaged"

    rows = store.fetch_llm_calls(wired_conn, role="triage_salience")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "parse_error"


# ---------------------------------------------------------------------------
# Fix 3: Gate parse-failure path records status="parse_error"
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_alert_gate_parse_failure_records_parse_error(tmp_path):
    """When parse_ok=False, the gate must record status='parse_error', not 'success'."""
    from tradingagents.orchestrator.alert_evaluator import record_alert_gate_llm_error

    conn = connect(str(tmp_path / "iic.db"))
    store.insert_event(
        conn,
        event_id="ev-parse",
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        salience=0.85,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="llm",
    )
    record_alert_gate_llm_error(
        conn,
        event_id="ev-parse",
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        status="parse_error",
        fallback_mode="none",
        fallback_used=False,
        parse_ok=False,
        exc=ValueError("alert_gate parse failure for event ev-parse"),
    )
    rows = store.fetch_llm_calls(conn, role="alert_gate")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "parse_error"
    assert row["parse_ok"] == 0  # stored as int in sqlite
    assert row["service_name"] == "promoter"
    assert row["linked_id"] == "ev-parse"


@pytest.mark.unit
def test_alert_gate_transport_error_records_transport_error(tmp_path):
    """Transport errors (LLM endpoint down) are recorded with status='transport_error'."""
    from tradingagents.orchestrator.alert_evaluator import record_alert_gate_llm_error

    conn = connect(str(tmp_path / "iic.db"))
    record_alert_gate_llm_error(
        conn,
        event_id=None,
        provider="local",
        model_id="qwen3.6-27b-instruct-q4_k_m",
        base_url="http://host.docker.internal:8080/v1",
        status="transport_error",
        fallback_mode="api",
        fallback_used=False,
        exc=ConnectionError("endpoint unreachable"),
    )
    rows = store.fetch_llm_calls(conn, role="alert_gate")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "transport_error"
    assert row["linked_id"] is None
    assert row["error_class"] == "ConnectionError"
