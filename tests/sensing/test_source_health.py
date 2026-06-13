import json
from unittest.mock import MagicMock, patch

import fakeredis.aioredis
import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
async def test_gdelt_success_updates_source_health(conn, tmp_path):
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter

    payload = {
        "articles": [{
            "url": "https://news.example/g-1",
            "title": "Macro shock",
            "seendate": "20260612T140000Z",
            "domain": "news.example",
        }],
    }
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status = lambda: None
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        adapter = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"), stream="ingest:raw")
        emitted = await adapter.poll_once(redis=redis, conn=conn)
    assert emitted == 1
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["service_name"] == "adapter-gdelt"
    assert row["last_success_ts"] is not None
    assert row["last_event_ts"] is not None
    assert row["cursor"] == "20260612T140000Z"
    assert row["events_emitted_last_poll"] == 1
    assert row["consecutive_failures"] == 0


@pytest.mark.unit
async def test_gdelt_failure_updates_source_health(conn, tmp_path):
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter

    with patch("tradingagents.sensing.adapters.gdelt.requests.get", side_effect=RuntimeError("boom")):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        adapter = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"), stream="ingest:raw")
        emitted = await adapter.poll_once(redis=redis, conn=conn)
    assert emitted == 0
    row = store.fetch_source_health(conn)["gdelt"]
    assert row["consecutive_failures"] == 1
    assert "boom" in row["last_error"]


@pytest.mark.unit
async def test_macro_fred_outage_records_failure(conn, tmp_path):
    """A FRED request exception must be recorded as a failure, not a success."""
    from tradingagents.sensing.adapters.macro import MacroAdapter

    with patch.dict("os.environ", {"FRED_API_KEY": "testkey"}):
        with patch("tradingagents.sensing.adapters.macro.requests.get",
                   side_effect=RuntimeError("boom")):
            redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            adapter = MacroAdapter(staging_root=str(tmp_path / "s"), stream="ingest:raw")
            emitted = await adapter.poll_once(redis=redis, conn=conn)

    assert emitted == 0
    row = store.fetch_source_health(conn)["macro"]
    assert row["consecutive_failures"] >= 1
    assert "boom" in row["last_error"]

    # A second consecutive failure should increment to 2.
    with patch.dict("os.environ", {"FRED_API_KEY": "testkey"}):
        with patch("tradingagents.sensing.adapters.macro.requests.get",
                   side_effect=RuntimeError("boom again")):
            redis2 = fakeredis.aioredis.FakeRedis(decode_responses=True)
            await adapter.poll_once(redis=redis2, conn=conn)

    row2 = store.fetch_source_health(conn)["macro"]
    assert row2["consecutive_failures"] == 2


@pytest.mark.unit
async def test_empty_poll_preserves_last_event_ts(conn, tmp_path):
    """An empty (zero-emit) poll must not overwrite last_event_ts from a prior success."""
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter

    payload_with_article = {
        "articles": [{
            "url": "https://news.example/g-1",
            "title": "First article",
            "seendate": "20260612T140000Z",
            "domain": "news.example",
        }],
    }
    m_hit = MagicMock()
    m_hit.json.return_value = payload_with_article
    m_hit.raise_for_status = lambda: None

    adapter = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"), stream="ingest:raw")

    # First poll — emits one article, sets last_event_ts.
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m_hit):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await adapter.poll_once(redis=redis, conn=conn)

    row_after_first = store.fetch_source_health(conn)["gdelt"]
    last_event_ts_first = row_after_first["last_event_ts"]
    assert last_event_ts_first is not None

    # Second poll — empty response (cursor will filter the seen article out).
    m_empty = MagicMock()
    m_empty.json.return_value = {"articles": []}
    m_empty.raise_for_status = lambda: None

    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m_empty):
        redis2 = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await adapter.poll_once(redis=redis2, conn=conn)

    row_after_second = store.fetch_source_health(conn)["gdelt"]
    assert row_after_second["last_event_ts"] == last_event_ts_first, (
        "empty poll must not overwrite last_event_ts"
    )
    assert row_after_second["events_emitted_last_poll"] == 0


@pytest.mark.unit
async def test_failure_then_success_resets_consecutive_failures(conn, tmp_path):
    """consecutive_failures must reset to 0 after a successful poll."""
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter

    adapter = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"), stream="ingest:raw")

    # First: failing poll.
    with patch("tradingagents.sensing.adapters.gdelt.requests.get",
               side_effect=RuntimeError("network error")):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await adapter.poll_once(redis=redis, conn=conn)

    row_fail = store.fetch_source_health(conn)["gdelt"]
    assert row_fail["consecutive_failures"] == 1

    # Second: successful poll.
    payload = {
        "articles": [{
            "url": "https://news.example/g-2",
            "title": "Recovery article",
            "seendate": "20260612T150000Z",
            "domain": "news.example",
        }],
    }
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status = lambda: None

    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m):
        redis2 = fakeredis.aioredis.FakeRedis(decode_responses=True)
        emitted = await adapter.poll_once(redis=redis2, conn=conn)

    assert emitted == 1
    row_success = store.fetch_source_health(conn)["gdelt"]
    assert row_success["consecutive_failures"] == 0


@pytest.mark.unit
async def test_telegram_message_records_channel_diagnostics(conn, tmp_path):
    from tradingagents.sensing.adapters.telegram import _on_message

    class Msg:
        message = "NVDA earnings leak"
        id = 7
        date = type("Date", (), {"isoformat": lambda self: "2026-06-12T10:00:00+00:00"})()

    class Chat:
        username = "earningswire"

    event = type("Event", (), {"message": Msg(), "chat": Chat()})()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await _on_message(event, redis=redis, conn=conn, stream="ingest:raw", staging_root=str(tmp_path / "s"))
    row = store.fetch_source_health(conn)["telegram"]
    diagnostics = json.loads(row["diagnostics"])
    assert diagnostics["resolved_channels"] == ["earningswire"]
    assert row["events_emitted_last_poll"] == 1
