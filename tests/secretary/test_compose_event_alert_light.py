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
def test_compose_light_delivers_to_channels_when_enabled(tmp_path, monkeypatch):
    from tradingagents.secretary import service as svc
    from tradingagents.secretary.service import Secretary
    conn = connect(str(tmp_path / "iic.db"))
    _seed_event(conn)
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="summary")
    sec = Secretary(conn=conn, data_dir=str(tmp_path / "data"), llm=llm)

    sent = []

    def _fake_send(**kw):
        sent.append(kw["mode"])
        # Under ordered policy, deliver_ordered reads the delivery row back from
        # the DB after each send — the fake must write a real row.
        brief = kw["brief"]
        return store.insert_delivery(
            conn,
            brief_id=brief["brief_id"],
            channel="fake",
            status="sent",
            sent_ts="2026-06-12T10:00:00+00:00",
            channel_ref="fake:1",
            skip_reason=None,
            delivery_group_id=kw.get("delivery_group_id"),
            attempt_rank=kw.get("attempt_rank"),
            fallback_of=kw.get("fallback_of"),
            is_fallback=kw.get("is_fallback", False),
        )

    fake_channel = MagicMock()
    fake_channel.channel_name = "fake"
    fake_channel.send.side_effect = _fake_send
    monkeypatch.setattr(svc, "_build_channel",
                        lambda name, conn, config: fake_channel)

    sec.compose_event_alert_light(event_id="ev1", tickers=["NVDA"],
                                  ttl_hours=24, deliver=True)
    # at least one channel.send happened, in event_alert_light mode
    assert "event_alert_light" in sent
