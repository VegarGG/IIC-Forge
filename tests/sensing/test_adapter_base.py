import json
import pytest
import fakeredis.aioredis
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.persistence.db import connect
from tradingagents.sensing.envelope import Envelope


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
async def test_envelope_writer_xadds_and_advances_cursor(conn, tmp_path):
    from tradingagents.sensing.adapters.base import EnvelopeWriter
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    w = EnvelopeWriter(source="polygon_news", redis=r, conn=conn,
                        stream="ingest:raw", staging_root=str(tmp_path / "staging"))
    env = Envelope(
        source="polygon_news",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="pn:1", text="Apple beats", source_tags={},
        raw_path="",
    )
    await w.write(env, raw_payload={"foo": "bar"}, cursor="2026-05-26T00:00:00Z")
    entries = await r.xrange("ingest:raw")
    assert len(entries) == 1
    _, fields = entries[0]
    data = json.loads(fields["data"])
    assert data["source"] == "polygon_news"
    assert data["raw_path"].endswith(".json")
    row = conn.execute("SELECT cursor FROM ingest_cursor WHERE source='polygon_news'").fetchone()
    assert row["cursor"] == "2026-05-26T00:00:00Z"
    from pathlib import Path
    assert Path(data["raw_path"]).exists()


@pytest.mark.unit
async def test_envelope_writer_fsyncs_aof_before_advancing_cursor(conn, tmp_path):
    from tradingagents.sensing.adapters.base import EnvelopeWriter

    calls = []

    class DurableRedis:
        async def xadd(self, stream, fields):
            calls.append(("xadd", stream, fields))
            return "1-0"

        async def execute_command(self, *args):
            calls.append(("waitaof", *args))
            return [1, 0]

    writer = EnvelopeWriter(
        source="rss",
        redis=DurableRedis(),
        conn=conn,
        stream="ingest:raw",
        staging_root=str(tmp_path / "staging"),
        require_aof_fsync=True,
        aof_fsync_timeout_ms=4321,
    )
    env = Envelope(
        source="rss",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="rss:durable",
        text="durable payload",
        source_tags={},
        raw_path="",
    )
    await writer.write(env, raw_payload={"text": "durable payload"}, cursor="c1")

    assert [call[0] for call in calls] == ["xadd", "waitaof"]
    assert calls[1][1:] == ("WAITAOF", 1, 0, 4321)
    row = conn.execute(
        "SELECT cursor FROM ingest_cursor WHERE source = 'rss'"
    ).fetchone()
    assert row["cursor"] == "c1"
    raw_path = Path(json.loads(calls[0][2]["data"])["raw_path"])
    assert raw_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
async def test_envelope_writer_does_not_advance_cursor_when_aof_fsync_fails(
    conn, tmp_path
):
    from tradingagents.sensing.adapters.base import EnvelopeWriter

    class UnconfirmedRedis:
        async def xadd(self, stream, fields):
            return "1-0"

        async def execute_command(self, *args):
            return [0, 0]

    writer = EnvelopeWriter(
        source="polygon_news",
        redis=UnconfirmedRedis(),
        conn=conn,
        stream="ingest:raw",
        staging_root=str(tmp_path / "staging"),
        require_aof_fsync=True,
    )
    env = Envelope(
        source="polygon_news",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="polygon:durability-timeout",
        text="payload",
        source_tags={},
        raw_path="",
    )
    with pytest.raises(RuntimeError, match="did not fsync"):
        await writer.write(env, raw_payload={"text": "payload"}, cursor="c1")
    assert conn.execute(
        "SELECT cursor FROM ingest_cursor WHERE source = 'polygon_news'"
    ).fetchone() is None


@pytest.mark.unit
def test_ingest_adapter_protocol_has_name_and_stream():
    from tradingagents.sensing.adapters.base import IngestAdapter
    annotations = IngestAdapter.__annotations__
    assert "name" in annotations
