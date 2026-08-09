"""RSS adapter — feedparser per feed, 5-min interval, per-feed cursors.

Cursor format: JSON dict mapping feed_url → max published ISO timestamp.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List

import feedparser
import redis.asyncio as aioredis

from tradingagents.sensing.adapters.base import EnvelopeWriter
from tradingagents.sensing.cursor import CursorStore
from tradingagents.sensing.envelope import Envelope
from tradingagents.security.untrusted import normalize_untrusted_text


log = logging.getLogger(__name__)
NAME = "rss"
POLL_INTERVAL = 5 * 60


def _entry_ts(entry) -> tuple[str, bool]:
    if getattr(entry, "published_parsed", None):
        # feedparser's struct_time is UTC. time.mktime interprets it in the
        # host timezone and shifts every cursor on non-UTC hosts.
        dt = datetime.fromtimestamp(
            calendar.timegm(entry.published_parsed), tz=timezone.utc
        )
        return dt.isoformat(), False
    return datetime.now(timezone.utc).isoformat(), True


class RssAdapter:
    name = NAME

    def __init__(
        self,
        *,
        feeds: List[str],
        staging_root: str,
        stream: str,
        require_aof_fsync: bool = False,
        aof_fsync_timeout_ms: int = 5000,
    ) -> None:
        self._feeds = list(feeds)
        self._staging = staging_root
        self._stream = stream
        self._require_aof_fsync = require_aof_fsync
        self._aof_fsync_timeout_ms = aof_fsync_timeout_ms

    def _load_cursor(self, conn) -> dict:
        cs = CursorStore(conn)
        raw = cs.get(NAME)
        return json.loads(raw) if raw else {}

    def _save_cursor(self, conn, d: dict) -> None:
        CursorStore(conn).set(NAME, json.dumps(d))

    async def poll_once(self, *, redis: aioredis.Redis, conn: sqlite3.Connection) -> int:
        cursors = self._load_cursor(conn)
        writer = EnvelopeWriter(source=NAME, redis=redis, conn=conn,
                                 stream=self._stream, staging_root=self._staging,
                                 require_aof_fsync=self._require_aof_fsync,
                                 aof_fsync_timeout_ms=self._aof_fsync_timeout_ms)
        emitted = 0
        for feed_url in self._feeds:
            try:
                feed = feedparser.parse(feed_url)
            except Exception as e:
                log.warning("rss parse failed for %s: %s", feed_url, e)
                continue
            if getattr(feed, "bozo", False):
                log.warning(
                    "rss feed %s reported malformed content: %s",
                    feed_url,
                    getattr(feed, "bozo_exception", "unknown parse error"),
                )
            last = cursors.get(feed_url, "")
            new_last = last
            ordered_entries = sorted(
                feed.entries,
                key=lambda item: _entry_ts(item)[0],
            )
            for entry in ordered_entries:
                ts, timestamp_inferred = _entry_ts(entry)
                if last and ts <= last:
                    continue
                raw_text = " ".join(filter(None, [
                    getattr(entry, "title", ""),
                    getattr(entry, "summary", ""),
                ]))
                text, _ = normalize_untrusted_text(raw_text, max_chars=20_000)
                stable_id = getattr(entry, "id", "") or getattr(entry, "link", "")
                if not stable_id:
                    stable_id = hashlib.sha256(
                        f"{feed_url}\0{ts}\0{text}".encode("utf-8")
                    ).hexdigest()
                ext_id = f"rss:{stable_id}"
                env = Envelope(
                    source=NAME,
                    ingested_ts=datetime.now(timezone.utc).isoformat(),
                    external_id=ext_id, text=text,
                    source_tags={
                        "feed": feed_url,
                        "link": getattr(entry, "link", ""),
                        "published_ts": ts,
                        "published_ts_inferred": timestamp_inferred,
                    },
                    raw_path="",
                )
                # Per-entry cursor is the feed-level dict, JSON-encoded.
                cursors[feed_url] = ts
                published = await writer.write(
                    env,
                    raw_payload={
                        "title": getattr(entry, "title", ""),
                        "summary": getattr(entry, "summary", ""),
                        "link": getattr(entry, "link", ""),
                        "published_ts": ts,
                    },
                    cursor=json.dumps(cursors),
                )
                if published:
                    emitted += 1
                new_last = ts
            cursors[feed_url] = max(new_last, last) if last else new_last
        self._save_cursor(conn, cursors)
        return emitted

    async def stream(self, *, redis, conn) -> None:
        backoff = 1
        while True:
            try:
                await self.poll_once(redis=redis, conn=conn)
                backoff = 1
            except Exception:
                log.exception("rss stream iteration crashed")
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(POLL_INTERVAL if backoff == 1 else backoff)


def _main() -> None:
    import os
    logging.basicConfig(level=logging.INFO)
    from tradingagents.default_config import DEFAULT_CONFIG as C
    from tradingagents.persistence.db import connect
    from tradingagents.sensing.redis_client import make_redis

    if not C["sensing_adapters_enabled"].get(NAME, True):
        log.info("%s disabled; exiting 0", NAME)
        return
    feeds = [f.strip() for f in os.environ.get("RSS_FEEDS", "").split(",") if f.strip()]
    if not feeds:
        log.warning("RSS_FEEDS env var not set; no feeds to poll")
    redis = make_redis(C["sensing_redis_url"])
    conn = connect(C["iic_db_path"])
    staging = os.path.join(C["iic_data_dir"], "events", "staging")
    a = RssAdapter(
        feeds=feeds,
        staging_root=staging,
        stream=C["sensing_ingest_stream"],
        require_aof_fsync=bool(C["sensing_require_aof_fsync"]),
        aof_fsync_timeout_ms=int(C["sensing_aof_fsync_timeout_ms"]),
    )
    asyncio.run(a.stream(redis=redis, conn=conn))


if __name__ == "__main__":
    _main()
