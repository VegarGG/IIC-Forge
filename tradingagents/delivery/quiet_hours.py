"""Quiet-hours scheduling for durable alert delivery.

Quiet hours apply to every automatic outbound placed in the delivery outbox,
including the morning digest. Rows are retained until the configured release
boundary; the transport worker performs a final predicate check. A digest
scheduled exactly at the 07:00 boundary is immediately eligible.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def configured_timezone(config: dict) -> ZoneInfo:
    name = str(config.get("timezone", "Asia/Shanghai"))
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown quiet-hours timezone: {name}") from exc


def is_quiet_hours(*, local_time: time, config: dict) -> bool:
    if not config.get("enabled", False):
        return False
    start = _parse_hhmm(config["start"])
    end = _parse_hhmm(config["end"])
    if start <= end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def next_allowed_utc(*, now_utc: datetime, config: dict) -> datetime:
    """Return the first UTC instant at which an alert may be delivered."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    normalized = now_utc.astimezone(timezone.utc)
    if not config.get("enabled", False):
        return normalized

    zone = configured_timezone(config)
    local_now = normalized.astimezone(zone)
    local_time = local_now.timetz().replace(tzinfo=None)
    if not is_quiet_hours(local_time=local_time, config=config):
        return normalized

    start = _parse_hhmm(config["start"])
    end = _parse_hhmm(config["end"])
    target_date = local_now.date()
    if start > end and local_time >= start:
        target_date += timedelta(days=1)
    target_local = datetime.combine(target_date, end, tzinfo=zone)
    return target_local.astimezone(timezone.utc)
