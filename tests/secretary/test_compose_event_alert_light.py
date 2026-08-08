import json
from unittest.mock import MagicMock
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


def _seed_event(conn):
    store.insert_event(conn, event_id="ev1", source="rss",
                       ingested_ts="2026-06-01T00:00:00+00:00", salience=0.9,
                       raw_path=None, status="triaged", deduped_of=None)


@pytest.mark.unit
def test_compose_light_creates_brief_actions_and_suppression(tmp_path):
    from tradingagents.secretary.service import Secretary
    conn = connect(str(tmp_path / "iic.db"))
    _seed_event(conn)
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="Short summary of the event.")
    sec = Secretary(conn=conn, data_dir=str(tmp_path / "data"), llm=llm)

    brief_id = sec.compose_event_alert_light(
        event_id="ev1", tickers=["NVDA", "PANW"], ttl_hours=24,
        deliver=False,
    )

    brief = store.get_brief(conn, brief_id=brief_id)
    assert brief["mode"] == "event_alert_light"
    assert sorted(json.loads(brief["scope"])) == ["NVDA", "PANW"]
    assert json.loads(brief["run_ids"]) == []
    assert brief["trigger_event_id"] == "ev1"

    actions = store.fetch_pending_run_full_study(conn)
    assert sorted(json.loads(a["action_params"])["ticker"] for a in actions) == ["NVDA", "PANW"]

    for t in ("NVDA", "PANW"):
        sup = conn.execute("SELECT * FROM suppression WHERE key=?",
                           (f"event_alert:{t}",)).fetchone()
        assert sup is not None
    # exactly one quick LLM call (the summary)
    assert llm.invoke.call_count == 1


@pytest.mark.unit
def test_compose_light_queues_enabled_channels(tmp_path):
    from tradingagents.secretary.service import Secretary
    conn = connect(str(tmp_path / "iic.db"))
    _seed_event(conn)
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="summary")
    sec = Secretary(conn=conn, data_dir=str(tmp_path / "data"), llm=llm)

    sec.compose_event_alert_light(event_id="ev1", tickers=["NVDA"],
                                  ttl_hours=24, deliver=True)
    rows = conn.execute(
        "SELECT channel, mode, state FROM delivery_queue ORDER BY channel"
    ).fetchall()
    assert [(r["channel"], r["mode"], r["state"]) for r in rows] == [
        ("email", "event_alert_light", "queued"),
        ("telegram", "event_alert_light", "queued"),
    ]


@pytest.mark.unit
def test_light_alert_bundle_rolls_back_if_outbox_enqueue_fails(
    tmp_path, monkeypatch
):
    from tradingagents.delivery import queue_store
    from tradingagents.secretary.service import Secretary

    conn = connect(str(tmp_path / "iic.db"))
    _seed_event(conn)
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="summary")
    sec = Secretary(conn=conn, data_dir=str(tmp_path / "data"), llm=llm)
    original = queue_store.enqueue_alert
    calls = 0

    def _fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced outbox failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(queue_store, "enqueue_alert", _fail_second)
    with pytest.raises(RuntimeError, match="forced outbox failure"):
        sec.compose_event_alert_light(
            event_id="ev1", tickers=["NVDA"], ttl_hours=24, deliver=True
        )

    assert conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM brief_actions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0] == 0
