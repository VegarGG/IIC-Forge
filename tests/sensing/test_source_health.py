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
