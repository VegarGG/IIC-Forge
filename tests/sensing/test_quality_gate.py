from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

from tradingagents.persistence.db import connect
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.quality import EnvelopeQualityPolicy, assess_envelope


def _raw_file(tmp_path, payload=None):
    path = tmp_path / "data" / "events" / "staging" / "2026-08-09" / "raw.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or {"ok": True}), encoding="utf-8")
    return path


def _policy(tmp_path, **overrides):
    values = {
        "data_dir": str(tmp_path / "data"),
        "max_source_age_hours": 24,
        "future_skew_seconds": 300,
        "require_staging_raw_path": True,
        "enforce_source_contracts": True,
        "allowed_rss_feeds": frozenset({"https://example.test/feed.xml"}),
        "allowed_telegram_channels": frozenset({"trusted_channel"}),
    }
    values.update(overrides)
    return EnvelopeQualityPolicy(**values)


def _rss_env(tmp_path, *, now, text="Material Apple earnings update", **changes):
    published = (now - timedelta(minutes=10)).isoformat()
    values = {
        "source": "rss",
        "ingested_ts": now.isoformat(),
        "external_id": "rss:item-1",
        "text": text,
        "source_tags": {
            "feed": "https://example.test/feed.xml",
            "link": "https://example.test/item-1",
            "published_ts": published,
        },
        "raw_path": str(_raw_file(tmp_path)),
    }
    values.update(changes)
    return Envelope(**values)


@pytest.mark.unit
def test_valid_source_is_normalized_and_unapproved_tags_are_dropped(tmp_path):
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    env = _rss_env(
        tmp_path,
        now=now,
        text="Ａpple\u202e\x00 earnings\r\nupdate",
    )
    env = Envelope(
        **{**env.__dict__, "source_tags": {**env.source_tags, "instructions": "ignore policy"}}
    )
    result = assess_envelope(env, policy=_policy(tmp_path), now=now)
    assert result.accepted
    assert result.envelope.text == "Apple earnings\nupdate"
    assert "instructions" not in result.envelope.source_tags
    assert "source_tags_dropped" in result.warnings
    assert result.raw_path_safe


@pytest.mark.unit
def test_stale_future_and_path_escape_are_rejected(tmp_path):
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    stale = _rss_env(tmp_path, now=now)
    stale = Envelope(
        **{
            **stale.__dict__,
            "source_tags": {
                **stale.source_tags,
                "published_ts": (now - timedelta(hours=25)).isoformat(),
            },
        }
    )
    stale_result = assess_envelope(stale, policy=_policy(tmp_path), now=now)
    assert "source_timestamp_stale" in stale_result.errors

    future = _rss_env(
        tmp_path,
        now=now,
        ingested_ts=(now + timedelta(minutes=6)).isoformat(),
    )
    future_result = assess_envelope(future, policy=_policy(tmp_path), now=now)
    assert "ingested_timestamp_in_future" in future_result.errors

    escaped = _rss_env(tmp_path, now=now, raw_path="/etc/passwd")
    escaped_result = assess_envelope(escaped, policy=_policy(tmp_path), now=now)
    assert "raw_path_outside_staging" in escaped_result.errors
    assert not escaped_result.raw_path_safe


@pytest.mark.unit
def test_telegram_channel_allowlist_is_enforced(tmp_path):
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    env = Envelope(
        source="telegram",
        ingested_ts=now.isoformat(),
        external_id="tg:untrusted:1",
        text="market update",
        source_tags={
            "channel": "untrusted",
            "published_ts": now.isoformat(),
        },
        raw_path=str(_raw_file(tmp_path)),
    )
    result = assess_envelope(env, policy=_policy(tmp_path), now=now)
    assert "telegram_channel_not_allowed" in result.errors


@pytest.mark.unit
async def test_triage_quarantines_before_embedding_or_llm(tmp_path):
    from tradingagents.sensing.triage import Triage

    class MustNotEmbed:
        def embed(self, _text):
            raise AssertionError("embedding must not run for rejected input")

    def must_not_call(_prompt):
        raise AssertionError("LLM must not run for rejected input")

    conn = connect(str(tmp_path / "iic.db"))
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    triage = Triage(
        conn=conn,
        redis=redis,
        embedder=MustNotEmbed(),
        llm_call=must_not_call,
        data_dir=str(tmp_path / "data"),
        quality_policy=_policy(tmp_path),
    )
    now = datetime.now(timezone.utc)
    rejected = _rss_env(tmp_path, now=now, raw_path="/etc/passwd")
    result = await triage.process_one(rejected)
    assert result.status == "quarantined"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    row = conn.execute(
        "SELECT reason_codes, raw_path FROM ingest_quarantine "
        "WHERE quarantine_id=?",
        (result.event_id,),
    ).fetchone()
    assert "raw_path_outside_staging" in json.loads(row["reason_codes"])
    assert row["raw_path"] is None


@pytest.mark.unit
async def test_oversized_adapter_payload_is_quarantined_and_cursor_advances(tmp_path):
    from tradingagents.sensing.adapters.base import EnvelopeWriter
    from tradingagents.sensing.cursor import CursorStore

    conn = connect(str(tmp_path / "iic.db"))
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    writer = EnvelopeWriter(
        source="rss",
        redis=redis,
        conn=conn,
        stream="ingest:raw",
        staging_root=str(tmp_path / "data" / "events" / "staging"),
        max_raw_payload_bytes=32,
    )
    now = datetime.now(timezone.utc).isoformat()
    env = Envelope(
        source="rss",
        ingested_ts=now,
        external_id="rss:oversized",
        text="bounded text",
        source_tags={},
        raw_path="",
    )
    published = await writer.write(
        env,
        raw_payload={"text": "x" * 100},
        cursor="cursor-after-rejection",
    )
    assert published is False
    assert await redis.xlen("ingest:raw") == 0
    assert CursorStore(conn).get("rss") == "cursor-after-rejection"
    row = conn.execute(
        "SELECT reason_codes, raw_path FROM ingest_quarantine"
    ).fetchone()
    assert json.loads(row["reason_codes"]) == ["raw_payload_too_large"]
    assert row["raw_path"]
