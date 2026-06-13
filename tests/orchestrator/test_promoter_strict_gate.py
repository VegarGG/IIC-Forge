from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

from tradingagents.persistence.db import connect
from tradingagents.persistence import store
from tradingagents.orchestrator.promoter import run_once


@dataclass
class EvalResult:
    passed: bool
    score: float
    payload: dict
    disqualifiers: list[str]
    # telemetry fields (Issue 1 fix requires these on the evaluation object)
    model_id: Optional[str] = None
    parse_ok: Optional[bool] = None
    latency_ms: Optional[int] = None


def seed_candidate(conn):
    store.upsert_watchlist(conn, ticker="NVDA", ttl_until=None, tags=["user"])
    store.insert_event(
        conn,
        event_id="ev1",
        source="rss",
        ingested_ts="2026-06-01T00:00:00+00:00",
        salience=0.95,
        raw_path=None,
        status="triaged",
        deduped_of=None,
    )
    store.insert_event_ticker(conn, event_id="ev1", ticker="NVDA", confidence=1.0)


def test_promoter_rejects_candidate_when_strict_gate_fails(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    seed_candidate(conn)
    secretary = MagicMock()

    n = run_once(
        conn,
        salience_threshold=0.85,
        ticker_conf_threshold=0.9,
        batch_size=50,
        cooldown_min=60,
        secretary=secretary,
        approval_gate_enabled=True,
        pending_ttl_hours=24,
        alert_evaluator=lambda event_id, tickers: EvalResult(
            passed=False,
            score=0.2,
            payload={"decision": "reject"},
            disqualifiers=["low_materiality"],
        ),
    )

    assert n == 0
    secretary.compose_event_alert_light.assert_not_called()


def test_promoter_composes_when_strict_gate_passes(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    seed_candidate(conn)
    secretary = MagicMock()

    n = run_once(
        conn,
        salience_threshold=0.85,
        ticker_conf_threshold=0.9,
        batch_size=50,
        cooldown_min=60,
        secretary=secretary,
        approval_gate_enabled=True,
        pending_ttl_hours=24,
        alert_evaluator=lambda event_id, tickers: EvalResult(
            passed=True,
            score=0.91,
            payload={"decision": "pass"},
            disqualifiers=[],
        ),
    )

    assert n == 1
    secretary.compose_event_alert_light.assert_called_once()


def test_promoter_threads_telemetry_into_insert(tmp_path):
    """Issue 1: run_once must pass model_id/parse_ok/latency_ms from the
    AlertEvaluation object into insert_alert_evaluation so the DB row is
    non-null for those columns."""
    conn = connect(str(tmp_path / "iic.db"))
    seed_candidate(conn)
    secretary = MagicMock()
    secretary.compose_event_alert_light.return_value = "lb1"

    run_once(
        conn,
        salience_threshold=0.85,
        ticker_conf_threshold=0.9,
        batch_size=50,
        cooldown_min=60,
        secretary=secretary,
        approval_gate_enabled=True,
        pending_ttl_hours=24,
        alert_evaluator=lambda event_id, tickers: EvalResult(
            passed=True,
            score=0.91,
            payload={"decision": "pass"},
            disqualifiers=[],
            model_id="test-model-v1",
            parse_ok=True,
            latency_ms=42,
        ),
    )

    row = conn.execute(
        "SELECT model_id, parse_ok, latency_ms FROM alert_evaluations LIMIT 1"
    ).fetchone()
    assert row is not None, "Expected an alert_evaluations row but found none"
    assert row["model_id"] == "test-model-v1"
    assert bool(row["parse_ok"]) is True
    assert row["latency_ms"] == 42
