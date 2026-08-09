"""Validation and normalization at the external-ingestion trust boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from tradingagents.security.untrusted import normalize_untrusted_text
from tradingagents.sensing.envelope import Envelope


APPROVED_SOURCES = frozenset({"rss", "telegram", "polygon_news"})
MAX_EVENT_TEXT_CHARS = 20_000
MAX_RAW_PAYLOAD_BYTES = 1_048_576
MAX_ENVELOPE_BYTES = 131_072
MAX_EXTERNAL_ID_CHARS = 512
MAX_SOURCE_TAG_BYTES = 8_192


@dataclass(frozen=True)
class EnvelopeQualityPolicy:
    data_dir: str
    approved_sources: frozenset[str] = APPROVED_SOURCES
    max_source_age_hours: int | None = None
    future_skew_seconds: int = 300
    max_event_text_chars: int = MAX_EVENT_TEXT_CHARS
    max_raw_payload_bytes: int = MAX_RAW_PAYLOAD_BYTES
    require_staging_raw_path: bool = False
    enforce_source_contracts: bool = False
    allowed_telegram_channels: frozenset[str] = frozenset()
    allowed_rss_feeds: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EnvelopeQualityAssessment:
    envelope: Envelope
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    raw_path_safe: bool

    @property
    def accepted(self) -> bool:
        return not self.errors


def _parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_scalar(value: Any, *, max_chars: int) -> str:
    return normalize_untrusted_text(value, max_chars=max_chars)[0]


def _sanitize_source_tags(source: str, tags: Any) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not isinstance(tags, dict):
        return {}, ["source_tags_not_object"]

    allowed = {
        "rss": {"feed", "link", "published_ts", "published_ts_inferred"},
        "telegram": {"channel", "published_ts"},
        "polygon_news": {"tickers", "publisher", "published_ts"},
    }.get(source, set())
    unknown = sorted(str(key) for key in tags if key not in allowed)
    if unknown:
        warnings.append("source_tags_dropped")

    sanitized: dict[str, Any] = {}
    for key in allowed:
        if key not in tags:
            continue
        value = tags[key]
        if key == "tickers":
            if not isinstance(value, (list, tuple)):
                warnings.append("tickers_not_list")
                continue
            sanitized[key] = [
                _normalize_scalar(item, max_chars=32).upper()
                for item in list(value)[:50]
                if _normalize_scalar(item, max_chars=32)
            ]
            if len(value) > 50:
                warnings.append("ticker_tags_truncated")
        elif key == "published_ts_inferred":
            sanitized[key] = bool(value)
        else:
            limit = 2_048 if key in {"feed", "link"} else 256
            sanitized[key] = _normalize_scalar(value, max_chars=limit)

    encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_SOURCE_TAG_BYTES:
        warnings.append("source_tags_oversized")
        sanitized = {
            key: value
            for key, value in sanitized.items()
            if key in {"published_ts", "published_ts_inferred", "channel", "publisher"}
        }
    return sanitized, warnings


def _path_is_safe(path_value: str, policy: EnvelopeQualityPolicy) -> tuple[bool, str | None]:
    if not policy.require_staging_raw_path:
        # Unit/library callers may construct envelopes without the production
        # staging contract. Never copy their arbitrary path, but do not reject
        # solely because strict production path enforcement was not enabled.
        return False, None
    if not path_value:
        return False, "raw_path_missing"
    staging = (Path(policy.data_dir).expanduser().resolve() / "events" / "staging")
    candidate = Path(path_value).expanduser().resolve()
    try:
        candidate.relative_to(staging)
    except ValueError:
        return False, "raw_path_outside_staging"
    if not candidate.is_file():
        return False, "raw_path_missing"
    try:
        if candidate.stat().st_size > policy.max_raw_payload_bytes:
            return False, "raw_payload_too_large"
    except OSError:
        return False, "raw_path_unreadable"
    return True, None


def assess_envelope(
    env: Envelope,
    *,
    policy: EnvelopeQualityPolicy,
    now: datetime | None = None,
) -> EnvelopeQualityAssessment:
    """Return a normalized envelope plus deterministic errors/warnings."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    errors: list[str] = []
    warnings: list[str] = []

    source = _normalize_scalar(env.source, max_chars=40).lower()
    if source not in policy.approved_sources:
        errors.append("source_not_approved")

    external_id = _normalize_scalar(env.external_id, max_chars=MAX_EXTERNAL_ID_CHARS + 1)
    if not external_id:
        errors.append("external_id_missing")
    elif len(external_id) > MAX_EXTERNAL_ID_CHARS:
        errors.append("external_id_too_long")

    text, truncated = normalize_untrusted_text(
        env.text, max_chars=policy.max_event_text_chars
    )
    if not text:
        errors.append("text_empty")
    if truncated:
        warnings.append("text_truncated")

    tags, tag_warnings = _sanitize_source_tags(source, env.source_tags)
    warnings.extend(tag_warnings)

    ingested = _parse_aware_timestamp(env.ingested_ts)
    if ingested is None:
        errors.append("ingested_timestamp_invalid")
        normalized_ingested = str(env.ingested_ts or "")
    else:
        normalized_ingested = ingested.isoformat()
        if ingested > current + timedelta(seconds=policy.future_skew_seconds):
            errors.append("ingested_timestamp_in_future")

    published_value = tags.get("published_ts")
    published = _parse_aware_timestamp(published_value)
    if published_value and published is None:
        errors.append("source_timestamp_invalid")
    if published is None:
        warnings.append("source_timestamp_missing")
        published = ingested
    if published is not None:
        if published > current + timedelta(seconds=policy.future_skew_seconds):
            errors.append("source_timestamp_in_future")
        if (
            policy.max_source_age_hours is not None
            and published < current - timedelta(hours=policy.max_source_age_hours)
        ):
            errors.append("source_timestamp_stale")

    if policy.enforce_source_contracts and source == "telegram":
        channel = str(tags.get("channel") or "").lstrip("@").lower()
        if not channel or channel == "unknown":
            errors.append("telegram_channel_missing")
        allowed = {item.lstrip("@").lower() for item in policy.allowed_telegram_channels}
        if allowed and channel not in allowed:
            errors.append("telegram_channel_not_allowed")
    elif policy.enforce_source_contracts and source == "rss":
        feed = str(tags.get("feed") or "")
        link = str(tags.get("link") or "")
        if not _safe_url(feed):
            errors.append("rss_feed_invalid")
        if link and not _safe_url(link):
            errors.append("rss_link_invalid")
        if policy.allowed_rss_feeds and feed not in policy.allowed_rss_feeds:
            errors.append("rss_feed_not_allowed")

    raw_path_safe, path_error = _path_is_safe(env.raw_path, policy)
    if path_error:
        errors.append(path_error)

    normalized = Envelope(
        source=source,
        ingested_ts=normalized_ingested,
        external_id=external_id,
        text=text,
        source_tags=tags,
        raw_path=env.raw_path if raw_path_safe else "",
    )
    return EnvelopeQualityAssessment(
        envelope=normalized,
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
        raw_path_safe=raw_path_safe,
    )


def parse_allowlist(value: str | Iterable[str]) -> frozenset[str]:
    values: Iterable[str]
    if isinstance(value, str):
        values = value.split(",")
    else:
        values = value
    return frozenset(str(item).strip() for item in values if str(item).strip())
