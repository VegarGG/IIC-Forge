import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


class FakeChannel:
    def __init__(self, *, conn, name, status):
        self._conn = conn
        self.channel_name = name
        self.status = status

    def send(self, *, brief, mode, body, delivery_group_id=None, attempt_rank=None, fallback_of=None, is_fallback=False):
        return store.insert_delivery(
            self._conn,
            brief_id=brief["brief_id"],
            channel=self.channel_name,
            status=self.status,
            sent_ts="2026-06-12T10:00:00+00:00" if self.status == "sent" else None,
            channel_ref=f"{self.channel_name}:1" if self.status == "sent" else None,
            skip_reason="quiet_hours" if self.status == "skipped" else None,
            delivery_group_id=delivery_group_id,
            attempt_rank=attempt_rank,
            fallback_of=fallback_of,
            is_fallback=is_fallback,
            failure_reason="failed" if self.status == "failed" else None,
        )


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        c,
        brief_id="b1",
        mode="event_alert_light",
        scope='["NVDA"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    return c


@pytest.mark.unit
def test_telegram_success_suppresses_email(conn):
    from tradingagents.delivery.policy import deliver_ordered

    result = deliver_ordered(
        conn=conn,
        brief={"brief_id": "b1", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg", "email": "em"},
        channels={
            "telegram": FakeChannel(conn=conn, name="telegram", status="sent"),
            "email": FakeChannel(conn=conn, name="email", status="sent"),
        },
        urgent=False,
    )
    assert result.final_status == "sent"
    groups = store.fetch_delivery_groups(conn)
    attempts = next(iter(groups.values()))
    assert [a["channel"] for a in attempts] == ["telegram"]


@pytest.mark.unit
def test_telegram_failure_triggers_email_fallback(conn):
    from tradingagents.delivery.policy import deliver_ordered

    result = deliver_ordered(
        conn=conn,
        brief={"brief_id": "b1", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg", "email": "em"},
        channels={
            "telegram": FakeChannel(conn=conn, name="telegram", status="failed"),
            "email": FakeChannel(conn=conn, name="email", status="sent"),
        },
        urgent=False,
    )
    assert result.final_status == "sent"
    attempts = next(iter(store.fetch_delivery_groups(conn).values()))
    assert [(a["channel"], a["status"], a["attempt_rank"]) for a in attempts] == [
        ("telegram", "failed", 1),
        ("email", "sent", 2),
    ]
    assert attempts[1]["fallback_of"] == attempts[0]["delivery_id"]
    assert attempts[1]["is_fallback"] == 1


@pytest.mark.unit
def test_quiet_hours_skip_does_not_email_unless_urgent(conn):
    from tradingagents.delivery.policy import deliver_ordered

    deliver_ordered(
        conn=conn,
        brief={"brief_id": "b1", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg", "email": "em"},
        channels={
            "telegram": FakeChannel(conn=conn, name="telegram", status="skipped"),
            "email": FakeChannel(conn=conn, name="email", status="sent"),
        },
        urgent=False,
    )
    attempts = next(iter(store.fetch_delivery_groups(conn).values()))
    assert [a["channel"] for a in attempts] == ["telegram"]
