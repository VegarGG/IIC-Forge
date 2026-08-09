"""Adapter contract + shared envelope-writing helper.

All adapters import EnvelopeWriter; the Protocol exists for documentation
and type-checking but is not strictly enforced at runtime.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import redis.asyncio as aioredis

from tradingagents.sensing.cursor import CursorStore
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.quality import (
    MAX_ENVELOPE_BYTES,
    MAX_RAW_PAYLOAD_BYTES,
)


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
    max_raw_payload_bytes: int = MAX_RAW_PAYLOAD_BYTES
    max_envelope_bytes: int = MAX_ENVELOPE_BYTES

    def __post_init__(self) -> None:
        self._cursor = CursorStore(self.conn)
        Path(self.staging_root).mkdir(parents=True, exist_ok=True)

    def _write_encoded(self, encoded: bytes, *, root: Path | None = None) -> str:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_dir = (root or Path(self.staging_root)) / day
        day_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = day_dir / f"{uuid.uuid4().hex}.json"
        temporary = day_dir / f".{path.name}.tmp"
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

    def _write_raw(self, encoded: bytes) -> str:
        return self._write_encoded(encoded)

    def _quarantine_before_publish(
        self,
        env: Envelope,
        *,
        reason: str,
        encoded_payload: bytes,
        cursor: str,
    ) -> None:
        from tradingagents.persistence.store import insert_ingest_quarantine

        observed = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(encoded_payload).hexdigest()
        quarantine_id = hashlib.sha256(
            f"{self.source}\0{env.external_id}\0{cursor}\0{reason}".encode("utf-8")
        ).hexdigest()
        metadata = json.dumps(
            {
                "reason": reason,
                "byte_count": len(encoded_payload),
                "sha256": digest,
            },
            sort_keys=True,
        ).encode("utf-8")
        quarantine_root = Path(self.staging_root).parent / "quarantine"
        raw_path = self._write_encoded(metadata, root=quarantine_root)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            insert_ingest_quarantine(
                self.conn,
                quarantine_id=quarantine_id,
                source=self.source,
                external_id=env.external_id or None,
                observed_ts=observed,
                reason_codes=[reason],
                raw_path=raw_path,
                envelope_sha256=digest,
                byte_count=len(encoded_payload),
                details={"stage": "adapter_pre_publish"},
                commit=False,
            )
            self._cursor.set(self.source, cursor, commit=False)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            Path(raw_path).unlink(missing_ok=True)
            raise

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
    ) -> bool:
        """Publish one bounded envelope; return False when quarantined locally."""
        encoded_payload = json.dumps(
            raw_payload, ensure_ascii=False, default=str
        ).encode("utf-8")
        if len(encoded_payload) > self.max_raw_payload_bytes:
            self._quarantine_before_publish(
                env,
                reason="raw_payload_too_large",
                encoded_payload=encoded_payload,
                cursor=cursor,
            )
            return False
        envelope_bytes = len(env.to_json().encode("utf-8"))
        if envelope_bytes > self.max_envelope_bytes:
            self._quarantine_before_publish(
                env,
                reason="envelope_too_large",
                encoded_payload=encoded_payload,
                cursor=cursor,
            )
            return False

        raw_path = self._write_raw(encoded_payload)
        # Envelope dataclass is frozen — rebuild with the real raw_path.
        env_with_path = Envelope(
            source=env.source, ingested_ts=env.ingested_ts,
            external_id=env.external_id, text=env.text,
            source_tags=env.source_tags, raw_path=raw_path,
        )
        await self.redis.xadd(self.stream, env_with_path.to_redis_fields())
        await self._require_stream_fsync()
        self._cursor.set(self.source, cursor)
        return True
