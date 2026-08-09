from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_base_send_event_alert_during_quiet_hours_queues(tmp_path):
    from tradingagents.delivery.base import DeliveryChannel

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn, brief_id="b1", mode="event_alert", scope="AAPL",
        generated_ts="2026-05-27T23:30:00+00:00",
        content_path="briefs/b1.md", run_ids=["r1"],
    )

    class Stub(DeliveryChannel):
        channel_name = "cli"
        def _send_impl(self, brief, mode, body):
            raise AssertionError("send_impl called during quiet hours")

    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"},
            "digest_modes": {"cli": "full"},
        },
    }

    with patch(
        "tradingagents.delivery.queue_store._utc_now",
        return_value=datetime(2026, 8, 8, 14, 30, tzinfo=timezone.utc),
    ):
        ch = Stub(conn=conn, config=cfg)
        delivery_id = ch.send(brief={"brief_id": "b1", "mode": "event_alert"},
                              mode="event_alert", body="...")

    row = conn.execute(
        "SELECT state, attempt_count, available_ts FROM delivery_queue "
        "WHERE delivery_job_id = ?",
        (delivery_id,),
    ).fetchone()
    assert row[0] == "queued"
    assert row[1] == 0
    assert row[2] == "2026-08-08T23:00:00+00:00"


@pytest.mark.unit
def test_base_send_morning_digest_uses_durable_quiet_hours_queue(tmp_path):
    from tradingagents.delivery.base import DeliveryChannel

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn, brief_id="b2", mode="morning_digest", scope='["AAPL"]',
        generated_ts="2026-05-27T23:30:00+00:00",
        content_path="briefs/b2.md", run_ids=["r1"],
    )

    class Stub(DeliveryChannel):
        channel_name = "cli"
        def _send_impl(self, brief, mode, body):
            raise AssertionError("queued digest must not call transport inline")

    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"},
            "digest_modes": {"cli": "full"},
        },
    }

    with patch(
        "tradingagents.delivery.queue_store._utc_now",
        return_value=datetime(2026, 8, 8, 14, 30, tzinfo=timezone.utc),
    ):
        ch = Stub(conn=conn, config=cfg)
        delivery_job_id = ch.send(
            brief={"brief_id": "b2", "mode": "morning_digest"},
            mode="morning_digest",
            body="...",
        )
    row = conn.execute(
        "SELECT mode, state, available_ts FROM delivery_queue "
        "WHERE delivery_job_id = ?",
        (delivery_job_id,),
    ).fetchone()
    assert tuple(row) == (
        "morning_digest",
        "queued",
        "2026-08-08T23:00:00+00:00",
    )


@pytest.mark.unit
def test_base_send_failure_recorded(tmp_path):
    from tradingagents.delivery.base import DeliveryChannel

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn, brief_id="b3", mode="deep_dive", scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b3.md", run_ids=["r1"],
    )

    class FailingStub(DeliveryChannel):
        channel_name = "email"
        def _send_impl(self, brief, mode, body):
            raise RuntimeError("smtp down")

    cfg = {"delivery": {"quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
                        "digest_modes": {"email": "full"}}}
    ch = FailingStub(conn=conn, config=cfg)
    delivery_id = ch.send(brief={"brief_id": "b3", "mode": "deep_dive"},
                          mode="deep_dive", body="...")
    row = conn.execute(
        "SELECT status, skip_reason, channel_ref FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] is None
    assert "smtp down" in (row[2] or "")
