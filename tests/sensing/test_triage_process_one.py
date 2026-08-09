import json
import pytest
import fakeredis.aioredis
from datetime import datetime, timezone

from tradingagents.persistence.db import connect
from tradingagents.persistence.store import upsert_ticker, get_active_watchlist
from tradingagents.sensing.envelope import Envelope


def _env(text="Apple reports a big beat on Q3 revenue", source="polygon_news",
         tags=None):
    return Envelope(
        source=source,
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id=f"x:{text[:5]}",
        text=text, source_tags=tags or {}, raw_path="data/events/staging/x.json",
    )


@pytest.fixture
def conn(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    upsert_ticker(conn, ticker="AAPL", exchange="NASDAQ",
                  name="Apple Inc.", aliases=[], active=True)
    return conn


def _make_llm(salience=0.9, conf=0.95, ticker="AAPL"):
    def call(_prompt):
        return json.dumps({
            "salience": salience,
            "matched_tickers": [ticker],
            "mentioned_tickers": [{"ticker": ticker, "confidence": conf}],
            "reason": "test",
        })
    return call


@pytest.mark.unit
async def test_process_one_writes_event_and_promotes(conn, tmp_path):
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(conn=conn, redis=r, embedder=MockEmbedder(),
               llm_call=_make_llm(),
               data_dir=str(tmp_path / "data"))
    res = await t.process_one(_env())
    assert res.status == "triaged"
    row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (res.event_id,)
    ).fetchone()
    assert row["salience"] == pytest.approx(0.9)
    et = conn.execute(
        "SELECT * FROM event_ticker WHERE event_id = ?", (res.event_id,)
    ).fetchone()
    assert et["ticker"] == "AAPL"
    assert "AAPL" in get_active_watchlist(conn)


@pytest.mark.unit
async def test_process_one_duplicate_does_not_promote(conn, tmp_path):
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(conn=conn, redis=r, embedder=MockEmbedder(),
               llm_call=_make_llm(),
               data_dir=str(tmp_path / "data"))
    env = _env(text="Same exact text", source="rss")
    res1 = await t.process_one(env)
    res2 = await t.process_one(env)  # exact replay
    assert res2.status == "duplicate"
    assert res2.deduped_of == res1.event_id
    n = conn.execute(
        "SELECT COUNT(*) FROM event_ticker WHERE event_id = ?", (res2.event_id,)
    ).fetchone()[0]
    assert n == 0


@pytest.mark.unit
async def test_process_one_rechecks_dedupe_inside_write_transaction(
    conn, tmp_path, monkeypatch
):
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    triage = Triage(
        conn=conn,
        redis=redis,
        embedder=MockEmbedder(),
        llm_call=_make_llm(),
        data_dir=str(tmp_path / "data"),
    )
    env = _env(text="Late duplicate race", source="rss")
    original = await triage.process_one(env)

    async def stale_stage1_miss(_env):
        return None

    monkeypatch.setattr(triage._ds1, "check", stale_stage1_miss)
    original_check_packed = triage._ds2.check_packed

    def stale_stage2_miss(packed, *, conn=None):
        if conn is None:
            return None
        return original_check_packed(packed, conn=conn)

    monkeypatch.setattr(triage._ds2, "check_packed", stale_stage2_miss)
    raced = await triage.process_one(env)

    assert raced.status == "duplicate"
    assert raced.deduped_of == original.event_id
    assert conn.execute(
        "SELECT COUNT(*) FROM event_embeddings"
    ).fetchone()[0] == 1


@pytest.mark.unit
async def test_process_one_drops_unknown_tickers(conn, tmp_path):
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(conn=conn, redis=r, embedder=MockEmbedder(),
               llm_call=_make_llm(ticker="NOTREAL"),
               data_dir=str(tmp_path / "data"))
    res = await t.process_one(_env())
    rows = conn.execute(
        "SELECT * FROM event_ticker WHERE event_id = ?", (res.event_id,)
    ).fetchall()
    assert rows == []


@pytest.mark.unit
async def test_process_one_below_threshold_no_promote(conn, tmp_path):
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(conn=conn, redis=r, embedder=MockEmbedder(),
               llm_call=_make_llm(salience=0.5),
               data_dir=str(tmp_path / "data"))
    res = await t.process_one(_env())
    assert res.status == "triaged"
    assert "AAPL" not in get_active_watchlist(conn)


@pytest.mark.unit
async def test_process_one_rolls_back_every_side_effect_and_preserves_staging(
    conn, tmp_path, monkeypatch
):
    from tradingagents.sensing import triage as triage_module
    from tradingagents.sensing.embeddings import MockEmbedder

    staging = tmp_path / "data" / "events" / "staging" / "source.json"
    staging.parent.mkdir(parents=True)
    staging.write_text('{"text":"Apple reports a big beat"}', encoding="utf-8")
    env = Envelope(
        source="rss",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="rss:atomic-failure",
        text="Apple reports a big beat",
        source_tags={"tickers": ["AAPL"]},
        raw_path=str(staging),
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    triage = triage_module.Triage(
        conn=conn,
        redis=redis,
        embedder=MockEmbedder(),
        llm_call=_make_llm(),
        data_dir=str(tmp_path / "data"),
    )

    def fail_after_ticker_link(*args, **kwargs):
        raise RuntimeError("forced watchlist failure")

    monkeypatch.setattr(triage_module, "auto_promote", fail_after_ticker_link)
    with pytest.raises(RuntimeError, match="forced watchlist failure"):
        await triage.process_one(env)

    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_fingerprints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_embeddings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vec_index").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_ticker").fetchone()[0] == 0
    assert staging.exists()
    canonical = (tmp_path / "data" / "events").glob("*.json")
    assert list(canonical) == []
