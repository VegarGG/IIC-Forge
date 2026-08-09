"""Beijing-time production scheduler for durable recurring work."""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import time
import uuid
from datetime import date, datetime, time as wall_time, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from tradingagents.orchestrator import queue_store
from tradingagents.persistence.db import connect
from tradingagents.sensing.watchlist import sweep_expired


log = logging.getLogger(__name__)
_shutdown = False


def morning_digest_brief_id(local_date: date) -> str:
    """Return the stable logical brief id for one Beijing calendar date."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://iic-forge.local/morning-digest/{local_date.isoformat()}",
    ).hex


def _schedule_time(config: Mapping[str, Any]) -> wall_time:
    raw = str((config.get("morning_digest") or {}).get("schedule_local_time", ""))
    try:
        parsed = wall_time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            "morning_digest.schedule_local_time must use HH:MM"
        ) from exc
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ValueError("morning_digest.schedule_local_time must use HH:MM")
    return parsed


def enqueue_due_morning_digest(
    conn: sqlite3.Connection,
    *,
    config: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Idempotently enqueue today's digest after its Beijing release time."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    timezone_name = str(
        ((config.get("delivery") or {}).get("quiet_hours") or {}).get(
            "timezone", "Asia/Shanghai"
        )
    )
    if timezone_name != "Asia/Shanghai":
        raise ValueError("production scheduler timezone must be Asia/Shanghai")
    zone = ZoneInfo(timezone_name)
    local_now = current.astimezone(zone)
    schedule = _schedule_time(config)
    if local_now.timetz().replace(tzinfo=None) < schedule:
        return None

    local_date = local_now.date()
    scheduled_ts = datetime.combine(local_date, schedule, tzinfo=zone)
    brief_id = morning_digest_brief_id(local_date)
    payload = json.dumps(
        {
            "brief_id": brief_id,
            "local_date": local_date.isoformat(),
            "scheduled_ts": scheduled_ts.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return queue_store.insert_queue_job(
        conn,
        job_type="morning_digest",
        payload=payload,
        trigger_event_id=None,
        idempotency_key=f"morning-digest:{timezone_name}:{local_date.isoformat()}",
        max_attempts=3,
    )


def _request_shutdown(_signum, _frame) -> None:
    global _shutdown
    _shutdown = True


def main(config: Optional[dict[str, Any]] = None) -> None:
    """Poll the daily schedule and perform hourly watchlist expiry sweeps."""
    from tradingagents.default_config import DEFAULT_CONFIG

    global _shutdown
    _shutdown = False
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    poll_seconds = max(
        float((cfg.get("morning_digest") or {}).get("scheduler_poll_seconds", 30)),
        1.0,
    )
    sweep_seconds = max(
        float((cfg.get("morning_digest") or {}).get("watchlist_sweep_seconds", 3600)),
        poll_seconds,
    )
    connection = connect(str(cfg["iic_db_path"]))
    last_sweep = 0.0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _request_shutdown)
    log.info(
        "scheduler started: morning=%s timezone=Asia/Shanghai poll=%ss",
        (cfg.get("morning_digest") or {}).get("schedule_local_time"),
        poll_seconds,
    )
    try:
        while not _shutdown:
            try:
                job_id = enqueue_due_morning_digest(connection, config=cfg)
                if job_id is not None:
                    log.debug("today's morning digest queue job is %d", job_id)
                monotonic_now = time.monotonic()
                if monotonic_now - last_sweep >= sweep_seconds:
                    removed = sweep_expired(connection)
                    if removed:
                        log.info("removed %d expired watchlist row(s)", removed)
                    last_sweep = monotonic_now
            except Exception:  # noqa: BLE001 - retry the durable schedule loop
                log.exception("scheduler cycle failed; retrying")
            if not _shutdown:
                time.sleep(poll_seconds)
    finally:
        connection.close()
        log.info("scheduler stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
