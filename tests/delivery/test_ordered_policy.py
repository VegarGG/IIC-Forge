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


@pytest.mark.unit
def test_disabled_telegram_falls_through_to_email_with_real_channels(tmp_path):
    """Disabled Telegram (telegram_disabled skip) must fall through to email.

    Uses real TelegramOutbound and EmailOutbound against a tmp DB, both in
    their disabled states, to verify the policy correctly distinguishes
    telegram_disabled from quiet_hours and does not short-circuit.
    """
    from tradingagents.delivery.policy import deliver_ordered
    from tradingagents.delivery.telegram import TelegramOutbound
    from tradingagents.delivery.email import EmailOutbound
    from tradingagents.persistence.db import connect as iic_connect

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b2",
        mode="event_alert_light",
        scope='["AAPL"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path="briefs/b2.md",
        run_ids=[],
    )

    # Telegram disabled: enabled=False and empty allowed_chat_ids.
    # This causes TelegramOutbound.send() to record skip_reason="telegram_disabled".
    cfg_telegram = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"telegram": "terse"},
        },
        "telegram_bot": {"enabled": False, "allowed_chat_ids": [], "poll_interval_seconds": 1},
    }
    # Email disabled: smtp.enabled=False.
    # This causes EmailOutbound.send() to record skip_reason="smtp_disabled".
    cfg_email = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"email": "full"},
        },
        "smtp": {
            "enabled": False,
            "host": "smtp.gmail.com",
            "port": 587,
            "from_addr": "x@example.com",
            "to_addrs": ["x@example.com"],
        },
    }

    tg_channel = TelegramOutbound(conn=conn, config=cfg_telegram)
    email_channel = EmailOutbound(conn=conn, config=cfg_email)

    result = deliver_ordered(
        conn=conn,
        brief={"brief_id": "b2", "mode": "event_alert_light"},
        mode="event_alert_light",
        bodies={"telegram": "tg body", "email": "email body"},
        channels={"telegram": tg_channel, "email": email_channel},
        urgent=False,
    )

    groups = store.fetch_delivery_groups(conn)
    assert result.delivery_group_id in groups
    attempts = groups[result.delivery_group_id]
    # Sort by attempt_rank for deterministic assertions.
    attempts_by_rank = sorted(attempts, key=lambda a: a["attempt_rank"])

    # Rank-1 row: telegram, skipped with telegram_disabled (NOT quiet_hours).
    rank1 = attempts_by_rank[0]
    assert rank1["channel"] == "telegram"
    assert rank1["attempt_rank"] == 1
    assert rank1["status"] == "skipped"
    assert rank1["skip_reason"] != "quiet_hours", (
        "telegram_disabled must not look like quiet_hours to the policy"
    )

    # Rank-2 row: email fallback EXISTS, proving fallthrough happened.
    assert len(attempts_by_rank) == 2, (
        "email fallback row must exist — disabled telegram should not suppress it"
    )
    rank2 = attempts_by_rank[1]
    assert rank2["channel"] == "email"
    assert rank2["attempt_rank"] == 2
    assert rank2["is_fallback"] == 1
