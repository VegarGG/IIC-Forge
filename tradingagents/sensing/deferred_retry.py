"""Durable retry workflow for deferred salience scoring."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.persistence import store
from tradingagents.sensing.envelope import Envelope

log = logging.getLogger(__name__)

# How long a row may stay in 'running' before it is re-pended (claimer died).
RECLAIM_RUNNING_AFTER_SECONDS = 1800


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _payload(env: Envelope) -> dict[str, Any]:
    return {
        "source": env.source,
        "ingested_ts": env.ingested_ts,
        "external_id": env.external_id,
        "text": env.text,
        "source_tags": env.source_tags,
        "raw_path": env.raw_path,
    }


def _hash_payload(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def envelope_from_payload(payload_json: str) -> Envelope:
    payload = json.loads(payload_json)
    return Envelope(
        source=payload["source"],
        ingested_ts=payload["ingested_ts"],
        external_id=payload.get("external_id") or "",
        text=payload["text"],
        source_tags=payload.get("source_tags") or {},
        raw_path=payload.get("raw_path") or "",
    )


def schedule_deferred_retry(
    conn: sqlite3.Connection,
    *,
    env: Envelope,
    event_id: str | None,
    reason: str,
    now_ts: str,
    base_delay_seconds: int,
) -> int:
    payload_json = json.dumps(_payload(env), sort_keys=True)
    ph = _hash_payload(payload_json)
    # Guard 2: dedupe on payload_hash — if a pending or running row already
    # exists for this exact payload, return its id without inserting a new one.
    # This prevents exponential row growth on Redis redelivery and on any other
    # call-site that races with an already-scheduled retry for the same event.
    existing_id = store.find_active_deferred_salience_retry(conn, payload_hash=ph)
    if existing_id is not None:
        log.debug(
            "schedule_deferred_retry: payload_hash %s already has active retry %d, "
            "skipping insert", ph[:16], existing_id,
        )
        return existing_id
    next_attempt = _iso(_parse(now_ts) + timedelta(seconds=base_delay_seconds))
    return store.insert_deferred_salience_retry(
        conn,
        event_id=event_id,
        source=env.source,
        raw_path=env.raw_path,
        payload_hash=ph,
        payload_json=payload_json,
        reason=reason,
        next_attempt_ts=next_attempt,
    )


def _next_attempt(
    now_ts: str,
    attempt_count: int,
    *,
    base_delay_seconds: int = 60,
    max_delay_seconds: int = 3600,
) -> str:
    """Compute the next attempt ISO timestamp using exponential backoff.

    ``attempt_count`` is the POST-increment value from the atomic claim
    (first claim returns attempt_count=1).  The delay is::

        delay = min(base_delay_seconds * 2 ** attempt_count, max_delay_seconds)

    So attempt_count=1 → delay=120s (2x base), attempt_count=2 → 240s, etc.
    """
    delay = min(base_delay_seconds * (2 ** max(attempt_count, 0)), max_delay_seconds)
    return _iso(_parse(now_ts) + timedelta(seconds=delay))


def reclaim_stale_running(conn: sqlite3.Connection, now_ts: str) -> int:
    """Re-pend 'running' rows older than RECLAIM_RUNNING_AFTER_SECONDS seconds."""
    cutoff = _iso(_parse(now_ts) - timedelta(seconds=RECLAIM_RUNNING_AFTER_SECONDS))
    return store.reclaim_stale_running_retries(conn, older_than_ts=cutoff)


async def run_due_retries_once(
    conn: sqlite3.Connection,
    *,
    triage,
    now_ts: str,
    limit: int,
    max_attempts: int,
) -> int:
    """Claim all due pending retries and run each through triage.process_one.

    Amendment A: claim returns POST-increment attempt_count rows.
    - Success (salience is not None) → mark done.
    - Failure and attempt_count >= max_attempts → mark dead.
    - Failure and attempt_count < max_attempts → reschedule with backoff.

    Returns the number of retry rows handled.
    """
    rows = store.claim_due_deferred_salience_retries(conn, now_ts=now_ts, limit=limit)
    handled = 0
    for row in rows:
        retry_id = int(row["retry_id"])
        attempt = int(row["attempt_count"])  # POST-increment: first claim = 1
        try:
            result = await triage.process_one(envelope_from_payload(row["payload_json"]), from_retry=True)
            handled += 1
            if getattr(result, "salience", None) is not None:
                store.mark_deferred_salience_retry_done(conn, retry_id=retry_id)
                continue
            # Still deferred (salience=None).
            if attempt >= max_attempts:
                store.mark_deferred_salience_retry_dead(
                    conn,
                    retry_id=retry_id,
                    reason="max_attempts_exhausted",
                )
            else:
                store.reschedule_deferred_salience_retry(
                    conn,
                    retry_id=retry_id,
                    reason="still_deferred",
                    next_attempt_ts=_next_attempt(now_ts, attempt),
                )
        except Exception as exc:  # noqa: BLE001
            handled += 1
            log.exception("deferred retry %d raised %s", retry_id, exc)
            if attempt >= max_attempts:
                store.mark_deferred_salience_retry_dead(
                    conn,
                    retry_id=retry_id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            else:
                store.reschedule_deferred_salience_retry(
                    conn,
                    retry_id=retry_id,
                    reason=f"{type(exc).__name__}: {exc}",
                    next_attempt_ts=_next_attempt(now_ts, attempt),
                )
    return handled
