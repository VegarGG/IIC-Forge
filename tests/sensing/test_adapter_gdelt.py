import json
import pytest
import fakeredis.aioredis
from unittest.mock import patch, MagicMock

from tradingagents.persistence.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
async def test_gdelt_emits_envelope(conn, tmp_path):
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter
    payload = {
        "articles": [{
            "url": "https://news.example/g-1",
            "title": "Macro shock",
            "seendate": "20260526T140000Z",
            "domain": "news.example",
        }],
    }
    m = MagicMock(); m.json.return_value = payload; m.raise_for_status = lambda: None
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m):
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        a = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"),
                          stream="ingest:raw")
        n = await a.poll_once(redis=r, conn=conn)
    assert n == 1
    entries = await r.xrange("ingest:raw")
    env = json.loads(entries[0][1]["data"])
    assert env["source"] == "gdelt"
    assert env["external_id"] == "gdelt:https://news.example/g-1"
    assert "Macro shock" in env["text"]


@pytest.mark.unit
async def test_gdelt_newest_first_cursor_advances_to_max_no_reemit(conn, tmp_path):
    """Regression (design §13): GDELT polls DateDesc (newest-first); the cursor
    must land on the MAX seendate of the batch so a repeat poll of the same
    rolling window emits nothing — DateAsc-era behavior re-emitted the window
    or went silent after the first poll."""
    from tradingagents.sensing.adapters.gdelt import GdeltAdapter
    from tradingagents.sensing.cursor import CursorStore

    payload = {
        "articles": [
            {"url": "https://news.example/g-new", "title": "Newest",
             "seendate": "20260612T150000Z", "domain": "news.example"},
            {"url": "https://news.example/g-mid", "title": "Middle",
             "seendate": "20260612T140000Z", "domain": "news.example"},
            {"url": "https://news.example/g-old", "title": "Oldest",
             "seendate": "20260612T130000Z", "domain": "news.example"},
        ],
    }
    m = MagicMock(); m.json.return_value = payload; m.raise_for_status = lambda: None
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m) as mock_get:
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        a = GdeltAdapter(query="earnings", staging_root=str(tmp_path / "s"),
                          stream="ingest:raw")
        n1 = await a.poll_once(redis=r, conn=conn)
        assert mock_get.call_args.kwargs["params"]["sort"] == "DateDesc"
    assert n1 == 3
    # Cursor must be the MAX seendate, not the last-iterated (oldest) one.
    assert CursorStore(conn).get("gdelt") == "20260612T150000Z"

    # Second poll over the same window: nothing new → zero emissions.
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m):
        n2 = await a.poll_once(redis=r, conn=conn)
    assert n2 == 0
    assert CursorStore(conn).get("gdelt") == "20260612T150000Z"

    # A later poll containing one strictly-newer article emits exactly that one.
    payload2 = {
        "articles": [
            {"url": "https://news.example/g-newer", "title": "Even newer",
             "seendate": "20260612T160000Z", "domain": "news.example"},
        ] + payload["articles"],
    }
    m2 = MagicMock(); m2.json.return_value = payload2; m2.raise_for_status = lambda: None
    with patch("tradingagents.sensing.adapters.gdelt.requests.get", return_value=m2):
        n3 = await a.poll_once(redis=r, conn=conn)
    assert n3 == 1
    assert CursorStore(conn).get("gdelt") == "20260612T160000Z"
