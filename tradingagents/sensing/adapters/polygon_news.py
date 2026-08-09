"""Polygon news adapter — REST poll every 60s.

Cursor: last-seen ``published_utc`` (ISO-8601 string). On each poll we
request items > cursor; if a poll returns nothing newer we just sleep
for the next interval.

Defensive: catches all requests errors; never raises out of stream().
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone

import redis.asyncio as aioredis
import requests

from tradingagents.sensing.adapters.base import EnvelopeWriter
from tradingagents.sensing.cursor import CursorStore
from tradingagents.sensing.envelope import Envelope
from tradingagents.security.untrusted import normalize_untrusted_text


log = logging.getLogger(__name__)
NAME = "polygon_news"
POLL_INTERVAL = 60  # seconds
MAX_CURSOR_LAG_HOURS = 6  # if last cursor older than this, resume from now-N


class PolygonNewsAdapter:
    name = NAME

    def __init__(
        self,
        *,
        staging_root: str,
        stream: str,
        require_aof_fsync: bool = False,
        aof_fsync_timeout_ms: int = 5000,
    ) -> None:
        self._staging = staging_root
        self._stream = stream
        self._require_aof_fsync = require_aof_fsync
        self._aof_fsync_timeout_ms = aof_fsync_timeout_ms

    def _api_key(self) -> str:
        k = os.environ.get("POLYGON_API_KEY")
        if not k:
            raise RuntimeError("POLYGON_API_KEY not set")
        return k

    def _resume_cursor(self, conn: sqlite3.Connection) -> str:
        cs = CursorStore(conn)
        existing = cs.get(NAME)
        if existing:
            return existing
        # Initial: backfill from "now minus 1 hour" to avoid a flood.
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    async def poll_once(self, *, redis: aioredis.Redis, conn: sqlite3.Connection) -> int:
        """One iteration of the loop. Returns # of envelopes emitted."""
        cursor = self._resume_cursor(conn)
        try:
            r = requests.get(
                "https://api.polygon.io/v2/reference/news",
                params={
                    "apiKey": self._api_key(),
                    "order": "asc",
                    "sort": "published_utc",
                    "published_utc.gt": cursor,
                    "limit": 100,
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("polygon poll failed (will retry): %s", e)
            return 0

        if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
            log.warning("polygon response rejected: results must be a list")
            return 0

        writer = EnvelopeWriter(source=NAME, redis=redis, conn=conn,
                                 stream=self._stream, staging_root=self._staging,
                                 require_aof_fsync=self._require_aof_fsync,
                                 aof_fsync_timeout_ms=self._aof_fsync_timeout_ms)
        emitted = 0
        items = [item for item in data.get("results", []) if isinstance(item, dict)]
        items.sort(key=lambda item: str(item.get("published_utc") or ""))
        for item in items:
            published = item.get("published_utc", "")
            if not published or published <= cursor:
                continue
            raw_text = " ".join(
                filter(None, [item.get("title", ""), item.get("description", "")])
            )
            text, _ = normalize_untrusted_text(raw_text, max_chars=20_000)
            stable_id = str(item.get("id") or "")
            if not stable_id:
                stable_id = hashlib.sha256(
                    f"{published}\0{text}".encode("utf-8")
                ).hexdigest()
            ext_id = f"pn:{stable_id}"
            env = Envelope(
                source=NAME,
                ingested_ts=datetime.now(timezone.utc).isoformat(),
                external_id=ext_id, text=text,
                source_tags={"tickers": item.get("tickers", []),
                             "publisher": (item.get("publisher") or {}).get("name", ""),
                             "published_ts": published},
                raw_path="",
            )
            if await writer.write(env, raw_payload=item, cursor=published):
                emitted += 1
        return emitted

    async def stream(self, *, redis: aioredis.Redis, conn: sqlite3.Connection) -> None:
        backoff = 1
        while True:
            try:
                await self.poll_once(redis=redis, conn=conn)
                backoff = 1
            except Exception:
                log.exception("polygon stream iteration crashed; backing off")
                backoff = min(backoff * 2, 60)
            await asyncio.sleep(POLL_INTERVAL if backoff == 1 else backoff)


def _main() -> None:
    """Systemd entry point."""
    logging.basicConfig(level=logging.INFO)
    from tradingagents.default_config import DEFAULT_CONFIG as C
    from tradingagents.persistence.db import connect
    from tradingagents.sensing.redis_client import make_redis

    if not C["sensing_adapters_enabled"].get(NAME, True):
        log.info("%s adapter disabled by config; exiting 0", NAME)
        return
    redis = make_redis(C["sensing_redis_url"])
    conn = connect(C["iic_db_path"])
    staging = os.path.join(C["iic_data_dir"], "events", "staging")
    a = PolygonNewsAdapter(
        staging_root=staging,
        stream=C["sensing_ingest_stream"],
        require_aof_fsync=bool(C["sensing_require_aof_fsync"]),
        aof_fsync_timeout_ms=int(C["sensing_aof_fsync_timeout_ms"]),
    )
    asyncio.run(a.stream(redis=redis, conn=conn))


if __name__ == "__main__":
    _main()
