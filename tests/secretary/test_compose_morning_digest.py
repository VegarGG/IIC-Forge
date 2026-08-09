import json
from unittest.mock import MagicMock, patch
import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_compose_morning_digest_writes_brief_and_per_ticker_sections(tmp_path):
    from tradingagents.secretary.service import Secretary

    conn = iic_connect(str(tmp_path / "iic.db"))
    data_dir = tmp_path / "data"

    store.upsert_watchlist(conn, ticker="AAPL", ttl_until=None, tags=["user"])
    store.upsert_watchlist(conn, ticker="MSFT", ttl_until=None, tags=["user"])

    fake_synthesis = {"consensus": "C", "divergence": "D",
                      "recommendation": "BUY", "raw": ""}

    def fake_run_ticker(*, ticker, trade_date, config, conn, data_dir):
        run_ids = []
        for p in ["macro", "value", "momentum"]:
            rid = f"r-{ticker}-{p}"
            store.insert_run(
                conn, run_id=rid, ticker=ticker, persona_id=p,
                started_ts="2026-05-27T07:00:00+00:00",
                artifact_dir=f"runs/{rid}",
            )
            store.finalize_run(
                conn, run_id=rid,
                ended_ts="2026-05-27T07:05:00+00:00",
                status="ok", decision="BUY", confidence=0.7,
            )
            run_ids.append(rid)
        return run_ids, fake_synthesis

    with patch("tradingagents.secretary.morning.run_one_ticker",
               side_effect=fake_run_ticker):
        sec = Secretary(conn=conn, data_dir=str(data_dir), llm=MagicMock())
        brief_id = sec.compose_morning_digest(
            watchlist=None, ts="2026-05-27T07:00:00+00:00",
        )

    row = conn.execute(
        "SELECT mode, scope, refine_depth FROM briefs WHERE brief_id = ?", (brief_id,)
    ).fetchone()
    assert row[0] == "morning_digest"
    assert sorted(json.loads(row[1])) == ["AAPL", "MSFT"]
    assert row[2] == 0

    content = (data_dir / "briefs" / f"{brief_id}.md").read_text()
    assert "AAPL" in content
    assert "MSFT" in content
    assert "BUY" in content


@pytest.mark.unit
def test_compose_morning_digest_continues_when_one_ticker_errors(tmp_path):
    from tradingagents.secretary.service import Secretary

    conn = iic_connect(str(tmp_path / "iic.db"))
    data_dir = tmp_path / "data"
    store.upsert_watchlist(conn, ticker="AAPL", ttl_until=None, tags=["user"])
    store.upsert_watchlist(conn, ticker="BAD", ttl_until=None, tags=["user"])

    def fake_run(*, ticker, trade_date, config, conn, data_dir):
        if ticker == "BAD":
            raise RuntimeError("graph crashed")
        rid = f"r-{ticker}"
        store.insert_run(conn, run_id=rid, ticker=ticker, persona_id="macro",
                         started_ts="2026-05-27T07:00:00+00:00",
                         artifact_dir=f"runs/{rid}")
        store.finalize_run(conn, run_id=rid,
                           ended_ts="2026-05-27T07:05:00+00:00",
                           status="ok", decision="BUY", confidence=0.6)
        return [rid], {"consensus": "C", "divergence": "D",
                       "recommendation": "BUY", "raw": ""}

    with patch("tradingagents.secretary.morning.run_one_ticker", side_effect=fake_run):
        sec = Secretary(conn=conn, data_dir=str(data_dir), llm=MagicMock())
        brief_id = sec.compose_morning_digest(
            watchlist=None, ts="2026-05-27T07:00:00+00:00",
        )
    content = (data_dir / "briefs" / f"{brief_id}.md").read_text()
    assert "AAPL" in content
    assert "data error" in content.lower()


@pytest.mark.unit
def test_compose_morning_digest_atomically_queues_exact_delivery_channels(tmp_path):
    from tradingagents.secretary.service import Secretary

    conn = iic_connect(str(tmp_path / "iic.db"))
    data_dir = tmp_path / "data"
    store.upsert_watchlist(conn, ticker="AAPL", ttl_until=None, tags=["user"])
    synthesis = {
        "consensus": "C",
        "divergence": "D",
        "recommendation": "BUY",
        "raw": "",
    }
    with patch(
        "tradingagents.secretary.morning.run_one_ticker",
        return_value=([], synthesis),
    ):
        sec = Secretary(conn=conn, data_dir=str(data_dir), llm=MagicMock())
        brief_id = sec.compose_morning_digest(
            watchlist=None,
            ts="2026-08-09T07:00:00+08:00",
            brief_id="daily-brief",
            deliver=True,
        )

    assert brief_id == "daily-brief"
    rows = conn.execute(
        "SELECT channel, mode, state, body FROM delivery_queue "
        "WHERE brief_id = ? ORDER BY channel",
        (brief_id,),
    ).fetchall()
    assert [(row["channel"], row["mode"], row["state"]) for row in rows] == [
        ("email", "morning_digest", "queued"),
        ("telegram", "morning_digest", "queued"),
    ]
    assert all("AAPL" in row["body"] for row in rows)


@pytest.mark.unit
def test_compose_morning_digest_retry_reuses_deterministic_brief(tmp_path):
    from tradingagents.secretary.service import Secretary

    conn = iic_connect(str(tmp_path / "iic.db"))
    sec = Secretary(conn=conn, data_dir=str(tmp_path / "data"), llm=MagicMock())
    with patch(
        "tradingagents.secretary.morning.run_one_ticker",
        return_value=([], {"consensus": "", "divergence": "", "recommendation": ""}),
    ) as runner:
        first = sec.compose_morning_digest(
            watchlist=["AAPL"],
            ts="2026-08-09T07:00:00+08:00",
            brief_id="stable-daily-id",
            deliver=True,
        )
        second = sec.compose_morning_digest(
            watchlist=["AAPL"],
            ts="2026-08-09T07:00:00+08:00",
            brief_id="stable-daily-id",
            deliver=True,
        )

    assert first == second == "stable-daily-id"
    assert runner.call_count == 1
    assert conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0] == 2
