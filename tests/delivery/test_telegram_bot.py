from unittest.mock import AsyncMock, MagicMock
import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


def _seed_brief_with_delivery(conn, brief_id="b1", channel_ref="12345:678"):
    store.insert_brief(
        conn,
        brief_id=brief_id,
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path=f"briefs/{brief_id}.md",
        run_ids=["r1"],
    )
    store.insert_delivery(
        conn,
        brief_id=brief_id,
        channel="telegram",
        status="sent",
        sent_ts="2026-05-27T12:00:01+00:00",
        channel_ref=channel_ref,
        skip_reason=None,
    )
    store.insert_brief_action(
        conn,
        brief_id=brief_id,
        action_type="run_backtest",
        action_params={},
        expires_at="2099-01-01T00:00:00+00:00",
    )


@pytest.mark.unit
def test_handle_callback_run_backtest_accepted(tmp_path):
    from tradingagents.delivery.telegram_bot import handle_callback

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_brief_with_delivery(conn)

    update = MagicMock()
    update.callback_query.data = "act:b1:run_backtest:yes"
    update.callback_query.message.chat.id = 12345
    update.callback_query.message.message_id = 678
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()

    handle_callback(update=update, conn=conn)

    row = conn.execute(
        "SELECT brief_id, action_type, state FROM brief_actions"
    ).fetchone()
    assert row[0] == "b1"
    assert row[1] == "run_backtest"
    assert row[2] == "accepted"


@pytest.mark.unit
def test_handle_callback_dismisses_pending_action(tmp_path):
    from tradingagents.delivery.telegram_bot import handle_callback

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_brief_with_delivery(conn)

    update = MagicMock()
    update.callback_query.data = "act:b1:run_backtest:no"
    update.callback_query.message.chat.id = 12345
    update.callback_query.message.message_id = 678
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()

    handle_callback(update=update, conn=conn)

    row = conn.execute("SELECT state FROM brief_actions").fetchone()
    assert row[0] == "declined"


@pytest.mark.unit
def test_handle_reply_creates_refine_action(tmp_path):
    from tradingagents.delivery.telegram_bot import handle_message

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_brief_with_delivery(conn)

    update = MagicMock()
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.chat.id = 12345
    update.message.reply_to_message.message_id = 678
    update.message.text = "more aggressive"
    update.message.chat.id = 12345

    handle_message(
        update=update, conn=conn, config={"refinement": {"action_expires_hours": 24}}
    )

    row = conn.execute(
        "SELECT brief_id, action_type, state, action_params FROM brief_actions "
        "WHERE action_type = 'refine_brief'"
    ).fetchone()
    assert row[0] == "b1"
    assert row[1] == "refine_brief"
    assert row[2] == "accepted"
    import json as _j

    assert _j.loads(row[3])["reply_text"] == "more aggressive"


@pytest.mark.unit
def test_handle_message_ignores_non_reply(tmp_path):
    from tradingagents.delivery.telegram_bot import handle_message

    conn = iic_connect(str(tmp_path / "iic.db"))
    update = MagicMock()
    update.message.reply_to_message = None
    update.message.text = "hello bot"
    update.message.chat.id = 12345

    handle_message(
        update=update, conn=conn, config={"refinement": {"action_expires_hours": 24}}
    )
    assert conn.execute("SELECT COUNT(*) FROM brief_actions").fetchone()[0] == 0


@pytest.mark.unit
def test_handle_callback_unknown_brief_id_does_nothing(tmp_path):
    from tradingagents.delivery.telegram_bot import handle_callback

    conn = iic_connect(str(tmp_path / "iic.db"))
    update = MagicMock()
    update.callback_query.data = "act:nonexistent:run_backtest:yes"
    update.callback_query.message.chat.id = 99999
    update.callback_query.message.message_id = 1
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()

    handle_callback(update=update, conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM brief_actions").fetchone()[0] == 0


@pytest.mark.unit
def test_expired_callback_cannot_be_resurrected(tmp_path):
    from tradingagents.delivery.telegram_bot import handle_callback

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_brief_with_delivery(conn)
    conn.execute("UPDATE brief_actions SET expires_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()

    update = MagicMock()
    update.callback_query.data = "act:b1:run_backtest:yes"
    update.callback_query.message.chat.id = 12345
    update.callback_query.message.message_id = 678
    update.callback_query.answer = AsyncMock()

    handle_callback(update=update, conn=conn)

    rows = conn.execute(
        "SELECT state, responded_at FROM brief_actions ORDER BY action_id"
    ).fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("expired", None)
    update.callback_query.answer.assert_awaited_once_with(
        text="Expired or already handled"
    )
