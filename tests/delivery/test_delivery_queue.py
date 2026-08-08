from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.delivery import queue_store
from tradingagents.delivery.base import DeliveryChannel
from tradingagents.delivery.worker import drain_one
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


QUIET = {
    "enabled": True,
    "start": "22:00",
    "end": "07:00",
    "timezone": "Asia/Shanghai",
}


def _config(*, quiet: bool = False) -> dict:
    return {
        "delivery": {
            "quiet_hours": {**QUIET, "enabled": quiet},
            "queue_max_attempts": 3,
            "queue_retry_base_seconds": 30,
            "queue_retry_cap_seconds": 300,
            "queue_lease_seconds": 120,
        },
        "smtp": {"enabled": False},
        "telegram_bot": {"enabled": False, "allowed_chat_ids": []},
    }


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        connection,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-08-08T00:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    return connection


def _enqueue(conn, *, now: datetime, max_attempts: int = 3) -> int:
    return queue_store.enqueue_alert(
        conn,
        brief_id="b1",
        channel="email",
        mode="event_alert",
        brief_payload={"brief_id": "b1", "mode": "event_alert"},
        body="alert body",
        quiet_hours=QUIET,
        max_attempts=max_attempts,
        now=now,
    )


@pytest.mark.unit
def test_quiet_hours_enqueue_releases_at_0700_beijing(conn):
    now = datetime(2026, 8, 8, 14, 30, tzinfo=timezone.utc)  # 22:30 Beijing
    job_id = _enqueue(conn, now=now)
    row = conn.execute(
        "SELECT available_ts FROM delivery_queue WHERE delivery_job_id = ?",
        (job_id,),
    ).fetchone()
    assert datetime.fromisoformat(row["available_ts"]) == datetime(
        2026, 8, 8, 23, 0, tzinfo=timezone.utc
    )
    assert queue_store.lease_one(conn, now=now) is None
    assert queue_store.lease_one(
        conn, now=datetime(2026, 8, 8, 23, 0, tzinfo=timezone.utc)
    )["delivery_job_id"] == job_id


@pytest.mark.unit
def test_enqueue_is_idempotent_and_rejects_changed_content(conn):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    first = _enqueue(conn, now=now)
    second = _enqueue(conn, now=now)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0] == 1

    with pytest.raises(ValueError, match="different content"):
        queue_store.enqueue_alert(
            conn,
            brief_id="b1",
            channel="email",
            mode="event_alert",
            brief_payload={"brief_id": "b1", "mode": "event_alert"},
            body="changed",
            quiet_hours=QUIET,
            now=now,
        )


@pytest.mark.unit
def test_transport_failure_retries_then_succeeds(conn):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    _enqueue(conn, now=now)
    outcomes = [RuntimeError("smtp unavailable"), None]

    class ControlledChannel(DeliveryChannel):
        channel_name = "email"

        def _send_impl(self, brief, mode, body):
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome
            return ("message-1", None)

    builder = lambda _name, connection, config: ControlledChannel(  # noqa: E731
        conn=connection, config=config
    )
    assert drain_one(conn, config=_config(), channel_builder=builder, now=now)
    retry = conn.execute("SELECT * FROM delivery_queue").fetchone()
    assert retry["state"] == "queued"
    assert retry["attempt_count"] == 1
    assert "smtp unavailable" in retry["last_error"]

    assert drain_one(
        conn,
        config=_config(),
        channel_builder=builder,
        now=now + timedelta(seconds=31),
    )
    sent = conn.execute("SELECT * FROM delivery_queue").fetchone()
    assert sent["state"] == "sent"
    assert sent["attempt_count"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM deliveries WHERE status = 'failed'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM deliveries WHERE status = 'sent'"
    ).fetchone()[0] == 1


@pytest.mark.unit
def test_retry_exhaustion_moves_delivery_to_dead(conn):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    _enqueue(conn, now=now, max_attempts=1)

    class FailingChannel(DeliveryChannel):
        channel_name = "email"

        def _send_impl(self, brief, mode, body):
            raise RuntimeError("permanent outage")

    builder = lambda _name, connection, config: FailingChannel(  # noqa: E731
        conn=connection, config=config
    )
    assert drain_one(conn, config=_config(), channel_builder=builder, now=now)
    row = conn.execute("SELECT state, last_error FROM delivery_queue").fetchone()
    assert row["state"] == "dead"
    assert "permanent outage" in row["last_error"]


@pytest.mark.unit
def test_expired_delivery_lease_is_recovered(conn):
    now = datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc)
    _enqueue(conn, now=now)
    leased = queue_store.lease_one(conn, lease_seconds=10, now=now)
    assert leased is not None
    assert queue_store.sweep_expired_leases(
        conn, now=now + timedelta(seconds=11)
    ) == 1
    row = conn.execute("SELECT state, last_error FROM delivery_queue").fetchone()
    assert row["state"] == "queued"
    assert row["last_error"] == "delivery_lease_expired"


@pytest.mark.unit
def test_two_delivery_workers_cannot_lease_the_same_row(tmp_path):
    path = tmp_path / "iic.db"
    setup = connect(str(path))
    store.insert_brief(
        setup,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-08-08T00:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    now = datetime.now(timezone.utc)
    queue_store.enqueue_alert(
        setup,
        brief_id="b1",
        channel="email",
        mode="event_alert",
        brief_payload={"brief_id": "b1", "mode": "event_alert"},
        body="body",
        quiet_hours={"enabled": False},
        now=now,
    )
    setup.close()

    def _lease():
        connection = connect(str(path))
        try:
            row = queue_store.lease_one(connection, now=now + timedelta(seconds=1))
            return row["delivery_job_id"] if row is not None else None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: _lease(), range(2)))
    assert sorted(result for result in results if result is not None) == [1]
