from datetime import datetime, timezone
from unittest.mock import patch

from tradingagents.delivery.base import DeliveryChannel
from tradingagents.persistence.db import connect
from tradingagents.persistence import store


class FakeChannel(DeliveryChannel):
    channel_name = "fake"

    def _send_impl(self, brief, mode, body):
        return ("fake:1", None)


def test_light_alert_queues_during_quiet_hours(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="light1",
        mode="event_alert_light",
        scope='["NVDA"]',
        generated_ts="2026-06-01T04:00:00+00:00",
        content_path="briefs/light1.md",
        run_ids=[],
    )
    ch = FakeChannel(
        conn=conn,
        config={
            "delivery": {
                "quiet_hours": {
                    "enabled": True,
                    "start": "22:00",
                    "end": "07:00",
                }
            },
            "brief_action_ttl_hours": 24,
        },
    )
    with patch(
        "tradingagents.delivery.queue_store._utc_now",
        return_value=datetime(2026, 8, 8, 14, 30, tzinfo=timezone.utc),
    ):
        delivery_id = ch.send(
            brief={"brief_id": "light1"},
            mode="event_alert_light",
            body="body",
        )
    row = conn.execute(
        "SELECT * FROM delivery_queue WHERE delivery_job_id = ?", (delivery_id,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["attempt_count"] == 0
    assert row["available_ts"] == "2026-08-08T23:00:00+00:00"
