"""Task 16: Cutover counters — soak-report helper.

Assert that ``soak_report(conn, ...)`` returns all counters needed for the
post-cutover soak period:

* ``local_gate_calls``       — alert_evaluations rows matching local_model_id
* ``gate_parse_failures``    — local gate rows where parse_ok = 0
                               (NULL rows excluded — counted as gate_parse_unknown)
* ``gate_parse_unknown``     — local gate rows where parse_ok IS NULL
* ``triage_events_scored``   — events with salience_source = 'llm'
* ``triage_events_cached``   — events with salience_source = 'cache'
* ``triage_events_total``    — scored + cached + deferred
* ``triage_events_deferred`` — events with salience_source = 'deferred'
* ``triage_llm_failures``    — ops_counter 'triage_llm_failures'
* ``promoter_llm_failures``  — ops_counter 'promoter_llm_failures'
* ``fallback_calls_today``   — {triage: int, promoter: int} for today's budget
* ``costs``                  — fetch_provider_split dict

Post-cutover expectations: failure counters = 0, api_spend in costs -> 0.
"""

from __future__ import annotations

import pytest

from tradingagents.persistence import store
from tradingagents.persistence.db import connect
from tradingagents.llm_clients.availability import (
    TRIAGE_FAILURE_COUNTER,
    PROMOTER_FAILURE_COUNTER,
    TRIAGE_FALLBACK_BUDGET,
    PROMOTER_FALLBACK_BUDGET,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_db(conn, *, local_model_id: str = "local-qwen3", day: str = "2026-06-10"):
    """Seed a minimal DB for the soak-report assertions."""
    # events: 3 scored by LLM, 1 cached, 1 deferred (salience LLM failed)
    for i in range(3):
        store.insert_event(
            conn,
            event_id=f"llm-evt-{i}",
            source="rss",
            ingested_ts=f"{day}T00:{i:02d}:00+00:00",
            salience=0.7 + i * 0.1,
            raw_path=None,
            status="triaged",
            deduped_of=None,
            salience_source="llm",
        )
    store.insert_event(
        conn,
        event_id="cache-evt-0",
        source="rss",
        ingested_ts=f"{day}T00:05:00+00:00",
        salience=0.75,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="cache",
    )
    store.insert_event(
        conn,
        event_id="defer-evt-0",
        source="rss",
        ingested_ts=f"{day}T00:10:00+00:00",
        salience=None,
        raw_path=None,
        status="deferred",
        deduped_of=None,
        salience_source="deferred",
    )

    # alert_evaluations: 2 local (1 parse_ok, 1 parse_fail), 1 API model
    store.insert_alert_evaluation(
        conn,
        event_id="llm-evt-0",
        tickers=["NVDA"],
        decision="pass",
        score=0.91,
        payload={},
        created_ts=f"{day}T00:00:30+00:00",
        model_id=local_model_id,
        parse_ok=True,
        latency_ms=120,
    )
    store.insert_alert_evaluation(
        conn,
        event_id="llm-evt-1",
        tickers=["TSLA"],
        decision="pass",
        score=0.85,
        payload={},
        created_ts=f"{day}T00:01:30+00:00",
        model_id=local_model_id,
        parse_ok=False,   # parse failure
        latency_ms=300,
    )
    # API-model evaluation (should NOT count toward local_gate_calls)
    store.insert_alert_evaluation(
        conn,
        event_id="llm-evt-2",
        tickers=["AAPL"],
        decision="pass",
        score=0.88,
        payload={},
        created_ts=f"{day}T00:02:30+00:00",
        model_id="deepseek-chat",   # API model
        parse_ok=True,
        latency_ms=800,
    )

    # ops_counters: non-zero failures to verify they are read (pre-cutover)
    store.bump_ops_counter(conn, name=TRIAGE_FAILURE_COUNTER, delta=5)
    store.bump_ops_counter(conn, name=PROMOTER_FAILURE_COUNTER, delta=3)

    # fallback-budget counters for today
    store.bump_ops_counter(conn, name=f"{TRIAGE_FALLBACK_BUDGET}:{day}", delta=2)
    store.bump_ops_counter(conn, name=f"{PROMOTER_FALLBACK_BUDGET}:{day}", delta=1)

    # costs: one local (free), one API with real spend
    store.insert_run(
        conn,
        run_id="run-local-1",
        ticker="NVDA",
        persona_id="balanced",
        started_ts=f"{day}T00:05:00+00:00",
        artifact_dir="runs/run-local-1",
    )
    store.record_cost(
        conn,
        run_id="run-local-1",
        provider="local",
        model=local_model_id,
        in_tokens=500,
        out_tokens=100,
        usd_estimate=0.0,
    )
    store.insert_run(
        conn,
        run_id="run-api-1",
        ticker="TSLA",
        persona_id="balanced",
        started_ts=f"{day}T00:06:00+00:00",
        artifact_dir="runs/run-api-1",
    )
    store.record_cost(
        conn,
        run_id="run-api-1",
        provider="deepseek",
        model="deepseek-chat",
        in_tokens=1000,
        out_tokens=200,
        usd_estimate=0.002,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_soak_report_returns_all_keys(tmp_path):
    """soak_report returns a dict with every expected soak-period counter."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    required_keys = {
        "local_gate_calls",
        "gate_parse_failures",
        "gate_parse_unknown",
        "triage_events_scored",
        "triage_events_cached",
        "triage_events_total",
        "triage_events_deferred",
        "triage_llm_failures",
        "promoter_llm_failures",
        "fallback_calls_today",
        "costs",
    }
    assert required_keys.issubset(report.keys()), (
        f"missing keys: {required_keys - set(report.keys())}"
    )


@pytest.mark.unit
def test_soak_report_local_gate_calls(tmp_path):
    """local_gate_calls = alert_evaluations rows matching local_model_id."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    # 2 rows for 'local-qwen3', 1 for 'deepseek-chat' — only local counted
    assert report["local_gate_calls"] == 2


@pytest.mark.unit
def test_soak_report_gate_parse_failures(tmp_path):
    """gate_parse_failures = local eval rows where parse_ok = 0."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    # 1 local eval has parse_ok=False
    assert report["gate_parse_failures"] == 1


@pytest.mark.unit
def test_soak_report_triage_volumes(tmp_path):
    """triage_events_scored / deferred = event salience_source counts."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    # 3 events with salience_source='llm', 1 with 'cache', 1 with 'deferred'
    assert report["triage_events_scored"] == 3
    assert report["triage_events_cached"] == 1
    assert report["triage_events_deferred"] == 1
    assert report["triage_events_total"] == 5  # 3 llm + 1 cache + 1 deferred


@pytest.mark.unit
def test_soak_report_failure_counters(tmp_path):
    """triage_llm_failures / promoter_llm_failures read from ops_counters."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    # seeded 5 triage failures and 3 promoter failures
    assert report["triage_llm_failures"] == 5
    assert report["promoter_llm_failures"] == 3


@pytest.mark.unit
def test_soak_report_fallback_calls_today(tmp_path):
    """fallback_calls_today.{triage,promoter} = daily budget counters."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    fb = report["fallback_calls_today"]
    assert fb["triage"] == 2
    assert fb["promoter"] == 1


@pytest.mark.unit
def test_soak_report_costs(tmp_path):
    """costs = fetch_provider_split dict with api_spend > 0 pre-cutover."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    costs = report["costs"]
    assert "local_calls" in costs
    assert "api_calls" in costs
    assert "api_spend" in costs
    # seeded 1 local cost row + 1 api cost row
    assert costs["local_calls"] == 1
    assert costs["api_calls"] == 1
    assert costs["api_spend"] == pytest.approx(0.002)


@pytest.mark.unit
def test_soak_report_post_cutover_zeros(tmp_path):
    """Post-cutover: all failures = 0, api_spend = 0.0, no API gate calls."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    # Only insert local-only data (no API calls, no failures)
    day = "2026-06-10"
    store.insert_event(
        conn,
        event_id="clean-evt-0",
        source="rss",
        ingested_ts=f"{day}T01:00:00+00:00",
        salience=0.9,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="llm",
    )
    store.insert_alert_evaluation(
        conn,
        event_id="clean-evt-0",
        tickers=["NVDA"],
        decision="pass",
        score=0.9,
        payload={},
        created_ts=f"{day}T01:00:30+00:00",
        model_id="local-qwen3",
        parse_ok=True,
        latency_ms=100,
    )

    report = soak_report(conn, local_model_id="local-qwen3", day=day)

    # Expected post-cutover happy path
    assert report["triage_llm_failures"] == 0
    assert report["promoter_llm_failures"] == 0
    assert report["gate_parse_failures"] == 0
    assert report["fallback_calls_today"]["triage"] == 0
    assert report["fallback_calls_today"]["promoter"] == 0
    assert report["costs"]["api_spend"] == pytest.approx(0.0)


@pytest.mark.unit
def test_soak_report_no_model_id_counts_all_evals(tmp_path):
    """When local_model_id=None, local_gate_calls counts all evaluations."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)

    report = soak_report(conn, local_model_id=None, day="2026-06-10")

    # Without a filter: 2 + 1 = 3 total alert_evaluations rows
    assert report["local_gate_calls"] == 3


# ---------------------------------------------------------------------------
# Fix 1: UTC day derivation
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_soak_report_utc_today_derivation(tmp_path):
    """soak_report(day=None) uses _utc_today() — UTC, not local time.

    Seed a budget key keyed by the SAME UTC date that _utc_today() returns,
    then call soak_report(day=None) and assert the counter is non-zero.
    This proves the UTC-day code path produces the same key the budget uses.
    """
    from datetime import datetime, timezone
    from scripts.f4_f5_exit_gate import soak_report, _utc_today
    from tradingagents.llm_clients.availability import TRIAGE_FALLBACK_BUDGET

    conn = connect(str(tmp_path / "iic.db"))
    utc_day = _utc_today()

    # Seed the budget counter under the UTC key
    store.bump_ops_counter(conn, name=f"{TRIAGE_FALLBACK_BUDGET}:{utc_day}", delta=7)

    report = soak_report(conn, day=None)

    assert report["fallback_calls_today"]["triage"] == 7, (
        f"Expected 7 fallback calls seeded for UTC day {utc_day!r}; "
        f"got {report['fallback_calls_today']['triage']} — "
        "possible UTC vs local date mismatch"
    )


@pytest.mark.unit
def test_utc_today_format():
    """_utc_today() returns a well-formed YYYY-MM-DD UTC date string."""
    import re
    from scripts.f4_f5_exit_gate import _utc_today
    from datetime import datetime, timezone

    result = _utc_today()

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", result), (
        f"_utc_today() must return YYYY-MM-DD, got {result!r}"
    )
    # Must equal datetime.now(timezone.utc).date().isoformat() (not local date)
    expected = datetime.now(timezone.utc).date().isoformat()
    assert result == expected


# ---------------------------------------------------------------------------
# Fix 2: NULL parse_ok excluded from failures; tracked as gate_parse_unknown
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_soak_report_null_parse_ok_not_counted_as_failure(tmp_path):
    """NULL parse_ok rows (pre-telemetry) must not count as gate_parse_failures.

    They should appear in gate_parse_unknown instead.
    """
    from scripts.f4_f5_exit_gate import soak_report

    day = "2026-06-10"
    conn = connect(str(tmp_path / "iic.db"))

    # Insert an event for the evaluation to reference
    store.insert_event(
        conn,
        event_id="pre-telem-evt-0",
        source="rss",
        ingested_ts=f"{day}T01:00:00+00:00",
        salience=0.8,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="llm",
    )
    # Insert a pre-telemetry evaluation: parse_ok=None -> NULL in DB
    store.insert_alert_evaluation(
        conn,
        event_id="pre-telem-evt-0",
        tickers=["NVDA"],
        decision="pass",
        score=0.8,
        payload={},
        created_ts=f"{day}T01:00:30+00:00",
        model_id="local-qwen3",
        parse_ok=None,   # pre-telemetry row — NULL in DB
        latency_ms=None,
    )
    # Also insert a real failure row so we can verify counts are distinct
    store.insert_event(
        conn,
        event_id="fail-evt-0",
        source="rss",
        ingested_ts=f"{day}T01:01:00+00:00",
        salience=0.5,
        raw_path=None,
        status="triaged",
        deduped_of=None,
        salience_source="llm",
    )
    store.insert_alert_evaluation(
        conn,
        event_id="fail-evt-0",
        tickers=["TSLA"],
        decision="pass",
        score=0.5,
        payload={},
        created_ts=f"{day}T01:01:30+00:00",
        model_id="local-qwen3",
        parse_ok=False,  # real parse failure
        latency_ms=200,
    )

    report = soak_report(conn, local_model_id="local-qwen3", day=day)

    # Only the explicit 0 row counts as failure; NULL row does NOT
    assert report["gate_parse_failures"] == 1, (
        f"Expected 1 parse failure (parse_ok=0), got {report['gate_parse_failures']}"
    )
    assert report["gate_parse_unknown"] == 1, (
        f"Expected 1 parse unknown (NULL), got {report['gate_parse_unknown']}"
    )
    assert report["local_gate_calls"] == 2


# ---------------------------------------------------------------------------
# Fix 3: triage_events_cached and triage_events_total present in report
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_soak_report_triage_cached_and_total(tmp_path):
    """triage_events_cached and triage_events_total are correct denominators."""
    from scripts.f4_f5_exit_gate import soak_report

    conn = connect(str(tmp_path / "iic.db"))
    _seed_db(conn)  # seeds 3 llm + 1 cache + 1 deferred events

    report = soak_report(conn, local_model_id="local-qwen3", day="2026-06-10")

    assert report["triage_events_cached"] == 1
    assert report["triage_events_total"] == 5  # 3 + 1 + 1
    # total must equal the sum of its parts
    assert report["triage_events_total"] == (
        report["triage_events_scored"]
        + report["triage_events_cached"]
        + report["triage_events_deferred"]
    )
