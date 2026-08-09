from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_telegram_outbound_sends_with_inline_keyboard_for_event_alert(tmp_path):
    from tradingagents.delivery.telegram import TelegramOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"telegram": "terse"},
        },
        "telegram_bot": {
            "enabled": True,
            "allowed_chat_ids": [12345],
            "poll_interval_seconds": 1,
        },
    }
    fake_sent = MagicMock(message_id=678)
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=fake_sent)
    with patch(
        "tradingagents.delivery.telegram._get_bot", return_value=fake_bot
    ), patch.dict("os.environ", {"IIC_TELEGRAM_BOT_TOKEN": "tok"}):
        ch = TelegramOutbound(conn=conn, config=cfg)
        delivery_id = ch.send_attempt(
            brief={"brief_id": "b1", "mode": "event_alert"},
            mode="event_alert",
            body="ALERT [AAPL](https://evil.invalid) _raw_!",
        )
    args, kwargs = fake_bot.send_message.call_args
    assert kwargs["chat_id"] == 12345
    assert kwargs["parse_mode"] == "MarkdownV2"
    assert kwargs["text"] == (r"ALERT \[AAPL\]\(https://evil\.invalid\) \_raw\_\!")
    assert kwargs.get("reply_markup") is not None
    row = conn.execute(
        "SELECT channel, status, channel_ref FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert row[0] == "telegram"
    assert row[1] == "sent"
    assert row[2] == "12345:678"


@pytest.mark.unit
def test_telegram_outbound_no_keyboard_for_morning_digest(tmp_path):
    from tradingagents.delivery.telegram import TelegramOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b2",
        mode="morning_digest",
        scope='["AAPL"]',
        generated_ts="2026-05-27T07:00:00+00:00",
        content_path="briefs/b2.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"telegram": "terse"},
        },
        "telegram_bot": {
            "enabled": True,
            "allowed_chat_ids": [12345],
            "poll_interval_seconds": 1,
        },
    }
    fake_sent = MagicMock(message_id=679)
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=fake_sent)
    with patch(
        "tradingagents.delivery.telegram._get_bot", return_value=fake_bot
    ), patch.dict("os.environ", {"IIC_TELEGRAM_BOT_TOKEN": "tok"}):
        ch = TelegramOutbound(conn=conn, config=cfg)
        ch.send(
            brief={"brief_id": "b2", "mode": "morning_digest"},
            mode="morning_digest",
            body="DIGEST TEXT",
        )
    kwargs = fake_bot.send_message.call_args.kwargs
    assert kwargs.get("reply_markup") is None


@pytest.mark.unit
def test_telegram_alert_disabled_records_permanent_block(tmp_path):
    from tradingagents.delivery.telegram import TelegramOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"telegram": "terse"},
        },
        "telegram_bot": {
            "enabled": False,
            "allowed_chat_ids": [],
            "poll_interval_seconds": 1,
        },
    }
    ch = TelegramOutbound(conn=conn, config=cfg)
    delivery_id = ch.send_attempt(
        brief={"brief_id": "b1", "mode": "event_alert"},
        mode="event_alert",
        body="...",
    )
    row = conn.execute(
        "SELECT status, skip_reason FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert row[0] == "blocked"
    assert row[1] == "channel_disabled"


@pytest.mark.unit
def test_telegram_markdown_escaping_respects_message_limit():
    pytest.importorskip("telegram")
    from tradingagents.delivery.telegram import _safe_markdown_text

    rendered = _safe_markdown_text("[x]!" * 5000)
    assert len(rendered) <= 4096
    assert rendered.endswith("…")
    assert not rendered[:-1].endswith("\\")
