import logging
import pytest
from datetime import datetime, timezone

from tradingagents.persistence.db import connect
from tradingagents.persistence import store
from tradingagents.orchestrator import queue_store as qs


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "iic.db"))
    store.insert_event(c, event_id="ev1", source="rss",
                       ingested_ts=datetime.now(timezone.utc).isoformat(),
                       salience=0.9, raw_path=None,
                       status="triaged", deduped_of=None)
    return c


# --------------------- QueueBackpressure ---------------------

@pytest.mark.unit
def test_backpressure_disabled_always_passes(conn, caplog):
    from tradingagents.orchestrator.guards import QueueBackpressure
    g = QueueBackpressure(enabled=False, max_pending=1)
    for _ in range(3):
        qs.insert_queue_job(conn, job_type="event_alert",
                            payload="{}", trigger_event_id="ev1")
    caplog.set_level(logging.DEBUG, logger="tradingagents.orchestrator.guards")
    assert g.gate(conn) is True
    # measurement-on policy: depth must appear in logs
    assert any("queue_depth=3" in r.message for r in caplog.records)


@pytest.mark.unit
def test_backpressure_enabled_gates_above_threshold(conn):
    from tradingagents.orchestrator.guards import QueueBackpressure
    g = QueueBackpressure(enabled=True, max_pending=2)
    for _ in range(2):
        qs.insert_queue_job(conn, job_type="event_alert",
                            payload="{}", trigger_event_id="ev1")
    assert g.gate(conn) is False   # 2 >= max=2 → gate closed


@pytest.mark.unit
def test_backpressure_enabled_passes_below_threshold(conn):
    from tradingagents.orchestrator.guards import QueueBackpressure
    g = QueueBackpressure(enabled=True, max_pending=5)
    qs.insert_queue_job(conn, job_type="event_alert",
                        payload="{}", trigger_event_id="ev1")
    assert g.gate(conn) is True


# --------------------- QueueRateGuard ---------------------

@pytest.mark.unit
def test_rate_disabled_passes_always(conn):
    from tradingagents.orchestrator.guards import QueueRateGuard
    g = QueueRateGuard(enabled=False, max_per_day=1)
    qs.insert_queue_job(conn, job_type="event_alert",
                        payload="{}", trigger_event_id="ev1")
    qs.insert_queue_job(conn, job_type="event_alert",
                        payload="{}", trigger_event_id="ev1")
    assert g.gate(conn) is True


@pytest.mark.unit
def test_rate_enabled_blocks_when_over_limit(conn):
    from tradingagents.orchestrator.guards import QueueRateGuard
    g = QueueRateGuard(enabled=True, max_per_day=2)
    for _ in range(2):
        qs.insert_queue_job(conn, job_type="event_alert",
                            payload="{}", trigger_event_id="ev1")
    assert g.gate(conn) is False


# --------------------- DailyBudgetGuard ---------------------

@pytest.mark.unit
def test_budget_disabled_never_blocks(conn):
    from tradingagents.orchestrator.guards import DailyBudgetGuard
    g = DailyBudgetGuard(enabled=False, daily_usd=1.00)
    qs.insert_queue_job(conn, job_type="event_alert",
                        payload="{}", trigger_event_id="ev1")
    job = qs.lease_one(conn)
    qs.mark_done(conn, job_id=job["job_id"], run_ids=[], brief_id=None,
                 cost_usd=10.0, lease_token=job["lease_token"])
    assert g.gate(conn) is True


@pytest.mark.unit
def test_budget_enabled_blocks_after_threshold(conn):
    from tradingagents.orchestrator.guards import DailyBudgetGuard
    from tradingagents.llm_clients.daily_budget import beijing_budget_date

    g = DailyBudgetGuard(enabled=True, daily_usd=5.00)
    budget_date = beijing_budget_date()
    # Accumulate $5 across paid calls, independent of queue-job boundaries.
    for _ in range(5):
        call_id = f"call-{_}"
        conn.execute(
            "INSERT INTO llm_budget_ledger ("
            "call_id, budget_date, budget_timezone, provider, model, state, "
            "reserved_usd, actual_usd, created_ts, settled_ts) "
            "VALUES (?, ?, 'Asia/Shanghai', 'deepseek', 'deepseek-v4-pro', "
            "'settled', 1.0, 1.0, '2026-08-09T00:00:00Z', "
            "'2026-08-09T00:00:01Z')",
            (call_id, budget_date),
        )
    conn.commit()
    assert g.gate(conn) is False
