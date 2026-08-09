"""Adapter contract + shared envelope-writing helper.

All adapters import EnvelopeWriter; the Protocol exists for documentation
and type-checking but is not strictly enforced at runtime.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import redis.asyncio as aioredis

from tradingagents.sensing.cursor import CursorStore
from tradingagents.sensing.envelope import Envelope


class IngestAdapter(Protocol):
    name: str  # "polygon_news", "telegram", ...

    async def stream(self, redis: aioredis.Redis, conn: sqlite3.Connection) -> None:
        """Long-lived. Reads from the source, writes envelopes to Redis,
        persists cursor after every successful batch. Defensively retry-internal."""


@dataclass
class EnvelopeWriter:
    """Writes raw payload to disk, XADDs envelope, advances cursor — atomically enough.

    The order is: write raw file → XADD envelope → set cursor. A crash between
    XADD and set-cursor results in the next adapter run re-fetching from the
    old cursor; the dedup pipeline tolerates the resulting re-deliveries.
    """
    source: str
    redis: aioredis.Redis
    conn: sqlite3.Connection
    stream: str
    staging_root: str
    require_aof_fsync: bool = False
    aof_fsync_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        self._cursor = CursorStore(self.conn)
        Path(self.staging_root).mkdir(parents=True, exist_ok=True)

    def _write_raw(self, payload: dict) -> str:
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_dir = Path(self.staging_root) / day
        day_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = day_dir / f"{uuid.uuid4().hex}.json"
        temporary = day_dir / f".{path.name}.tmp"
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(day_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(path)

    async def _require_stream_fsync(self) -> None:
        """Wait until the local Redis AOF contains the preceding XADD.

        ``WAITAOF 1 0`` is available in Redis 7.2+. A timeout or a Redis
        instance without AOF is fatal for this envelope: the SQLite cursor is
        deliberately left unchanged, so the source can safely redeliver it.
        """
        if not self.require_aof_fsync:
            return
        result = await self.redis.execute_command(
            "WAITAOF", 1, 0, int(self.aof_fsync_timeout_ms)
        )
        if not isinstance(result, (list, tuple)) or not result:
            raise RuntimeError(f"Redis WAITAOF returned an invalid result: {result!r}")
        try:
            local_fsyncs = int(result[0])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Redis WAITAOF returned an invalid local count: {result!r}"
            ) from exc
        if local_fsyncs < 1:
            raise RuntimeError(
                "Redis did not fsync the ingestion stream before the durability timeout"
            )

    async def write(
        self,
        env: Envelope,
        *,
        raw_payload: dict,
        cursor: str,
    ) -> None:
        raw_path = self._write_raw(raw_payload)
        # Envelope dataclass is frozen — rebuild with the real raw_path.
        env_with_path = Envelope(
            source=env.source, ingested_ts=env.ingested_ts,
            external_id=env.external_id, text=env.text,
            source_tags=env.source_tags, raw_path=raw_path,
        )
        await self.redis.xadd(self.stream, env_with_path.to_redis_fields())
        await self._require_stream_fsync()
        self._cursor.set(self.source, cursor)
