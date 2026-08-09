"""Job dispatcher — routes leased jobs to the right handler.

Today only ``event_alert`` is supported. The DISPATCH map is the seam
F5 (morning_digest) extends.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict


log = logging.getLogger(__name__)


class JobBlockedError(ValueError):
    """A job cannot succeed without operator/configuration correction."""

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


def _event_alert_payload(raw_payload: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise JobBlockedError(
            "event_alert payload is not valid JSON",
            category="invalid_payload",
        ) from exc
    if not isinstance(payload, dict):
        raise JobBlockedError(
            "event_alert payload must be a JSON object",
            category="invalid_payload",
        )
    for field in ("event_id", "ticker"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JobBlockedError(
                f"event_alert payload requires non-empty {field}",
                category="invalid_payload",
            )
    return payload


def dispatch_event_alert(
    conn: sqlite3.Connection,
    job: Dict[str, Any],
    *,
    secretary,                           # tradingagents.secretary.service.Secretary
) -> Dict[str, Any]:
    """Run an event_alert job. Returns the rollup dict the worker writes
    into queue_jobs (brief_id, run_ids JSON, cost_usd)."""
    payload = _event_alert_payload(job["payload"])
    event_id = payload["event_id"]
    ticker = payload["ticker"]
    action_id = payload.get("action_id")
    parent_brief_id = payload.get("parent_brief_id")
    job_id = job["job_id"]

    if conn.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
    ).fetchone() is None:
        raise JobBlockedError(
            f"event_alert references missing event {event_id!r}",
            category="missing_event",
        )

    # Link the full brief back to the light alert for the same event, if any.
    if not parent_brief_id:
        parent_row = conn.execute(
            "SELECT brief_id FROM briefs WHERE mode = 'event_alert_light' "
            "AND trigger_event_id = ? ORDER BY generated_ts DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        parent_brief_id = parent_row[0] if parent_row else None

    brief_id = secretary.compose_event_alert(
        event_id=event_id, ticker=ticker, job_id=job_id,
        parent_brief_id=parent_brief_id, deliver=True,
    )

    # Pull run_ids back from the brief row (compose_event_alert wrote them).
    brief = conn.execute(
        "SELECT run_ids FROM briefs WHERE brief_id = ?", (brief_id,)
    ).fetchone()
    run_ids = json.loads(brief["run_ids"]) if brief and brief["run_ids"] else []

    # Cost rollup: sum usd_estimate across all runs in this job.
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        row = conn.execute(
            f"SELECT COALESCE(SUM(usd_estimate), 0) "
            f"FROM costs WHERE run_id IN ({placeholders})",
            tuple(run_ids),
        ).fetchone()
        cost_usd = float(row[0])
    else:
        cost_usd = 0.0

    from tradingagents.persistence import store
    if action_id is not None:
        store.mark_action_done(
            conn,
            action_id=int(action_id),
            result_brief_id=brief_id,
        )
    else:
        store.mark_full_study_action_done_for_job(
            conn,
            job_id=job_id,
            result_brief_id=brief_id,
        )

    return {"brief_id": brief_id, "run_ids": run_ids, "cost_usd": cost_usd}


DISPATCH = {
    "event_alert": dispatch_event_alert,
    # F5 will add: "morning_digest": dispatch_morning_digest
}


def dispatch(
    conn: sqlite3.Connection,
    job: Dict[str, Any],
    *,
    secretary,
) -> Dict[str, Any]:
    handler = DISPATCH.get(job["job_type"])
    if handler is None:
        raise JobBlockedError(
            f"unknown job_type: {job['job_type']!r}",
            category="unknown_job_type",
        )
    return handler(conn, job, secretary=secretary)
