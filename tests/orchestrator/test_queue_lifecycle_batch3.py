from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.orchestrator import queue_store
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "iic.db"))
    store.insert_event(
        connection,
        event_id="ev1",
        source="rss",
        ingested_ts="2026-08-08T00:00:00+00:00",
        salience=0.9,
        raw_path=None,
        status="triaged",
        deduped_of=None,
    )
    return connection


def _insert(
    conn,
    *,
    key: str = "event:ev1:AAPL",
    max_attempts: int = 3,
    available: datetime | None = None,
) -> int:
    return queue_store.insert_queue_job(
        conn,
        job_type="event_alert",
        payload=json.dumps({"event_id": "ev1", "ticker": "AAPL"}),
        trigger_event_id="ev1",
        idempotency_key=key,
        max_attempts=max_attempts,
        available_ts=available.isoformat() if available else None,
    )


@pytest.mark.unit
def test_idempotent_enqueue_returns_existing_job(conn):
    first = _insert(conn)
    second = _insert(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0] == 1


@pytest.mark.unit
def test_idempotency_key_cannot_be_reused_for_different_payload(conn):
    _insert(conn)
    with pytest.raises(ValueError, match="different queue job"):
        queue_store.insert_queue_job(
            conn,
            job_type="event_alert",
            payload=json.dumps({"event_id": "ev1", "ticker": "MSFT"}),
            trigger_event_id="ev1",
            idempotency_key="event:ev1:AAPL",
        )


@pytest.mark.unit
def test_failure_uses_backoff_then_exhausts(conn):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    job_id = _insert(conn, max_attempts=2, available=now)
    first = queue_store.lease_one(conn, now=now)
    state = queue_store.mark_failure(
        conn,
        job_id=job_id,
        error_msg="temporary",
        lease_token=first["lease_token"],
        retry_base_seconds=30,
        retry_cap_seconds=60,
        now=now,
    )
    assert state == "queued"
    assert queue_store.lease_one(conn, now=now + timedelta(seconds=29)) is None

    second = queue_store.lease_one(conn, now=now + timedelta(seconds=30))
    state = queue_store.mark_failure(
        conn,
        job_id=job_id,
        error_msg="still failing",
        lease_token=second["lease_token"],
        now=now + timedelta(seconds=30),
    )
    assert state == "error"
    row = conn.execute("SELECT * FROM queue_jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["attempt_count"] == 2
    assert row["finished_ts"] is not None


@pytest.mark.unit
def test_stale_token_cannot_complete_released_job(conn):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    job_id = _insert(conn, available=now)
    first = queue_store.lease_one(conn, lease_seconds=10, now=now)
    assert queue_store.sweep_stale_leases(
        conn, max_age_seconds=3600, now=now + timedelta(seconds=9)
    ) == 0
    assert queue_store.sweep_stale_leases(
        conn, max_age_seconds=3600, now=now + timedelta(seconds=11)
    ) == 1

    with pytest.raises(queue_store.QueueLeaseLost):
        queue_store.mark_done(
            conn,
            job_id=job_id,
            run_ids=[],
            brief_id=None,
            cost_usd=0,
            lease_token=first["lease_token"],
        )


@pytest.mark.unit
def test_expired_but_unreplaced_token_can_acknowledge_completion(conn):
    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)
    job_id = _insert(conn, available=long_ago)
    lease = queue_store.lease_one(conn, lease_seconds=1, now=long_ago)

    queue_store.mark_done(
        conn,
        job_id=job_id,
        run_ids=[],
        brief_id=None,
        cost_usd=0,
        lease_token=lease["lease_token"],
    )

    row = conn.execute(
        "SELECT state, lease_token FROM queue_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert row["state"] == "done"
    assert row["lease_token"] is None
