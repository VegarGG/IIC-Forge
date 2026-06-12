import json
import pytest
import fakeredis.aioredis
from unittest.mock import MagicMock
from pathlib import Path

from tradingagents.persistence.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
async def test_telegram_handler_emits_envelope(conn, tmp_path):
    from tradingagents.sensing.adapters.telegram import _on_message
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    ev = MagicMock()
    ev.message.id = 42
    ev.message.message = "Apple breaks above resistance"
    ev.message.date.isoformat.return_value = "2026-05-26T12:00:00+00:00"
    ev.chat.username = "iic_signals"

    await _on_message(ev, redis=r, conn=conn,
                      stream="ingest:raw",
                      staging_root=str(tmp_path / "s"))

    entries = await r.xrange("ingest:raw")
    assert len(entries) == 1
    env = json.loads(entries[0][1]["data"])
    assert env["source"] == "telegram"
    assert env["external_id"] == "tg:iic_signals:42"
    assert "Apple breaks above resistance" in env["text"]
    cur = conn.execute("SELECT cursor FROM ingest_cursor WHERE source='telegram'").fetchone()
    d = json.loads(cur["cursor"])
    assert d.get("iic_signals") == 42


@pytest.mark.unit
async def test_telegram_handler_skips_empty_messages(conn, tmp_path):
    from tradingagents.sensing.adapters.telegram import _on_message
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ev = MagicMock()
    ev.message.id = 1
    ev.message.message = "   "  # whitespace-only
    ev.message.date.isoformat.return_value = "2026-05-26T12:00:00+00:00"
    ev.chat.username = "iic"
    await _on_message(ev, redis=r, conn=conn,
                      stream="ingest:raw",
                      staging_root=str(tmp_path / "s"))
    assert await r.xlen("ingest:raw") == 0


@pytest.mark.unit
def test_ensure_session_dir_creates_missing_parent(tmp_path):
    """_ensure_session_dir must create a missing parent so the adapter does not crash-loop."""
    from tradingagents.sensing.adapters.telegram import _ensure_session_dir

    session_path = str(tmp_path / "telegram" / "iic_sensing.session")
    parent = Path(session_path).parent
    assert not parent.exists(), "precondition: parent dir must not exist yet"

    _ensure_session_dir(session_path)

    assert parent.exists(), "_ensure_session_dir must create the parent directory"
    assert parent.is_dir()


@pytest.mark.unit
def test_ensure_session_dir_idempotent(tmp_path):
    """Calling _ensure_session_dir twice must not raise."""
    from tradingagents.sensing.adapters.telegram import _ensure_session_dir

    session_path = str(tmp_path / "deep" / "nested" / "iic_sensing.session")
    _ensure_session_dir(session_path)
    _ensure_session_dir(session_path)  # must not raise
    assert Path(session_path).parent.is_dir()
