"""Envelope: the single message shape on the ``ingest:raw`` Redis stream.

Adapters construct ``Envelope`` instances and call ``redis.xadd(stream, env.to_redis_fields())``.
The triage consumer reverses with ``Envelope.from_redis_fields(fields)``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping


_WHITESPACE_RE = re.compile(r"\s+")


class EnvelopeDecodeError(ValueError):
    """A Redis envelope is malformed and must be quarantined, not retried."""


def normalize_for_fingerprint(text: str) -> str:
    """Whitespace-collapsed, lowercased text for SHA-256 dedup hashing.

    Identical wording with different whitespace / casing must hash equal.
    """
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


@dataclass(frozen=True)
class Envelope:
    source: str            # "polygon_news", "telegram", "x", "rss", "gdelt", "macro"
    ingested_ts: str       # ISO-8601 UTC, e.g. "2026-05-26T14:33:21.123Z"
    external_id: str       # source-supplied stable ID; empty string if unavailable
    text: str              # normalized full text the LLM and embedder see
    source_tags: Dict[str, Any]  # e.g. {"tickers": ["AAPL"], "category": "earnings"}
    raw_path: str          # filesystem path under data/events/staging/...

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, blob: str) -> "Envelope":
        try:
            payload = json.loads(blob)
        except (json.JSONDecodeError, TypeError) as exc:
            raise EnvelopeDecodeError("envelope data is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise EnvelopeDecodeError("envelope JSON must be an object")
        required = {"source", "ingested_ts", "external_id", "text", "source_tags", "raw_path"}
        if set(payload) != required:
            raise EnvelopeDecodeError("envelope JSON fields do not match the contract")
        scalar_fields = ("source", "ingested_ts", "external_id", "text", "raw_path")
        if any(not isinstance(payload[field], str) for field in scalar_fields):
            raise EnvelopeDecodeError("envelope scalar fields must be strings")
        if not isinstance(payload["source_tags"], dict):
            raise EnvelopeDecodeError("envelope source_tags must be an object")
        return cls(**payload)

    def to_redis_fields(self) -> Dict[str, str]:
        # One field carries the whole JSON. Keeps XADD payload simple and avoids
        # collisions with Redis-reserved field names.
        return {"data": self.to_json()}

    @classmethod
    def from_redis_fields(
        cls, fields: Mapping[str | bytes, str | bytes]
    ) -> "Envelope":
        # Redis returns bytes when decode_responses=False; tolerate both.
        data = fields.get("data")
        if data is None:
            data = fields.get(b"data")
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if not isinstance(data, str):
            raise EnvelopeDecodeError("Redis envelope is missing string field 'data'")
        return cls.from_json(data)
