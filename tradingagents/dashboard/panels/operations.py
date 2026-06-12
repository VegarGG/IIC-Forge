"""Operational status queries shared by dashboard and focused gates.

No streamlit import at module level — this module is imported headlessly by
the focused soak gate as well as the dashboard.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from tradingagents.persistence import store
from tradingagents.orchestrator import queue_store
from tradingagents.dashboard.panels.costs import fetch_provider_split


def _dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _age_seconds(now_ts: str, ts: str | None) -> float | None:
    now = _dt(now_ts)
    other = _dt(ts)
    if now is None or other is None:
        return None
    return (now - other).total_seconds()


def fetch_llm_role_summary(
    conn: sqlite3.Connection,
    *,
    now_ts: str | None = None,
    window_seconds: int = 86400,
) -> dict[str, dict[str, Any]]:
    """Aggregate LLM call stats grouped by role.

    When *now_ts* is provided, only rows whose created_ts falls within the
    most recent *window_seconds* seconds are included.  datetime() SQL
    normalization is used on both sides so that the ``+00:00``-suffixed ISO
    strings written by ledger._now_iso() compare correctly with the cutoff.

    When *now_ts* is omitted the query is all-time (backward-compatible).
    """
    if now_ts is not None:
        cutoff_dt = _dt(now_ts) - timedelta(seconds=window_seconds)
        cutoff_iso = cutoff_dt.isoformat()
        rows = conn.execute(
            "SELECT role, COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status = 'parse_error' THEN 1 ELSE 0 END) AS parse_failures, "
            "SUM(CASE WHEN status IN ('transport_error', 'timeout') THEN 1 ELSE 0 END) AS transport_failures, "
            "SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_used, "
            "AVG(latency_ms) AS avg_latency_ms "
            "FROM llm_calls "
            "WHERE datetime(created_ts) >= datetime(?) "
            "GROUP BY role",
            (cutoff_iso,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status = 'parse_error' THEN 1 ELSE 0 END) AS parse_failures, "
            "SUM(CASE WHEN status IN ('transport_error', 'timeout') THEN 1 ELSE 0 END) AS transport_failures, "
            "SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_used, "
            "AVG(latency_ms) AS avg_latency_ms "
            "FROM llm_calls GROUP BY role"
        ).fetchall()
    return {r["role"]: dict(r) for r in rows}


def fetch_deferred_summary(
    conn: sqlite3.Connection,
    *,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Return per-state counts for deferred_salience_retry plus orphaned event count.

    Amendment B: an orphaned event is one with salience_source = 'deferred' and
    salience IS NULL where no retry row exists in state pending/running/done.
    A successfully-retried event's original deferred row keeps salience NULL
    while its retry row transitions to 'done', so 'done' is included in the
    NOT EXISTS clause to avoid counting recovered events as orphans.

    When *now_ts* is provided, ``oldest_pending_age_seconds`` is added: the age
    (in seconds) of the oldest state='pending' row, measured from its
    *created_ts*.  None when no pending rows exist or when now_ts is not given.
    (Design §11: "Deferred salience queue depth, oldest pending age".)
    """
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM deferred_salience_retry GROUP BY state"
    ).fetchall()
    by_state: dict[str, Any] = {r["state"]: int(r["n"]) for r in rows}

    orphan_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM events e
        WHERE e.salience_source = 'deferred' AND e.salience IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM deferred_salience_retry r
              WHERE r.event_id = e.event_id
                AND r.state IN ('pending', 'running', 'done')
          )
        """
    ).fetchone()
    by_state["orphaned_events"] = int(orphan_row["cnt"]) if orphan_row else 0

    if now_ts is not None:
        oldest_row = conn.execute(
            "SELECT MIN(created_ts) AS oldest_ts FROM deferred_salience_retry "
            "WHERE state = 'pending'"
        ).fetchone()
        oldest_ts = oldest_row["oldest_ts"] if oldest_row else None
        by_state["oldest_pending_age_seconds"] = _age_seconds(now_ts, oldest_ts)
    else:
        by_state["oldest_pending_age_seconds"] = None

    return by_state


def fetch_failed_delivery_groups(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[dict]:
    """Return groups where no delivery was sent AND at least one attempt failed.

    Amendment A: a quiet-hours-skipped group (sent=0, all skipped by policy)
    is NOT a failed group — it must have at least one status='failed' attempt
    to count here.

    At most *limit* rows are returned (default 50).  Use
    ``count_failed_delivery_groups`` to obtain the total count.
    """
    rows = conn.execute(
        """
        SELECT delivery_group_id,
               COUNT(*) AS attempts,
               SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_attempts
        FROM deliveries
        WHERE delivery_group_id IS NOT NULL
        GROUP BY delivery_group_id
        HAVING sent = 0 AND failed_attempts >= 1
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_failed_delivery_groups(conn: sqlite3.Connection) -> int:
    """Return the total count of failed delivery groups (no sent, ≥1 failed attempt).

    Use alongside ``fetch_failed_delivery_groups`` when you need the unbounded
    total for display (e.g. dashboard metric) while keeping the list capped.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM (
            SELECT delivery_group_id,
                   SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_attempts
            FROM deliveries
            WHERE delivery_group_id IS NOT NULL
            GROUP BY delivery_group_id
            HAVING sent = 0 AND failed_attempts >= 1
        )
        """
    ).fetchone()
    return int(row["cnt"]) if row else 0


def fetch_skipped_only_delivery_group_count(conn: sqlite3.Connection) -> int:
    """Count groups where sent=0 and no failed attempt exists (all skipped by policy).

    Amendment A: these are intentionally not alarms — they're policy-skipped groups.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM (
            SELECT delivery_group_id,
                   SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_attempts
            FROM deliveries
            WHERE delivery_group_id IS NOT NULL
            GROUP BY delivery_group_id
            HAVING sent = 0 AND failed_attempts = 0
        )
        """
    ).fetchone()
    return int(row["cnt"]) if row else 0


def fetch_operations_snapshot(
    conn: sqlite3.Connection, *, now_ts: str
) -> dict[str, Any]:
    """Build the full operational evidence snapshot.

    Consumed by both the dashboard Operations tab and the focused soak gate.
    No side effects; no streamlit imports.

    Returned keys
    -------------
    sources : dict[str, dict]
        Per-source health rows from ``source_health``, keyed by source name.
        Each value is the raw store dict plus two injected age fields:
        ``last_poll_age_seconds`` and ``last_event_age_seconds`` (float or None).

    llm_calls : dict[str, dict]
        Per-role LLM call statistics for the 24-hour window ending at *now_ts*.
        Keys are role strings; values have ``total``, ``success``,
        ``parse_failures``, ``transport_failures``, ``fallback_used``,
        ``avg_latency_ms``.  Uses a rolling 86 400-second window; call
        ``fetch_llm_role_summary`` without *now_ts* for all-time data.

    deferred_salience : dict[str, Any]
        Per-state counts from ``deferred_salience_retry`` (keys are state
        strings), plus ``orphaned_events`` (int) and
        ``oldest_pending_age_seconds`` (float or None — age of the oldest
        state='pending' row measured from its created_ts).

    queue_lanes : dict[str, dict]
        Lane-level job depth from ``queue_store.lane_depth``.

    delivery_groups : dict
        ``failed``       — list of capped (≤50) failed-group dicts.
        ``failed_total`` — int, total count of all failed groups (unbounded).
        ``skipped_only`` — int, groups with no sent and no failed attempt.

    costs : dict
        Run-scoped spend breakdown from the ``costs`` table (provider split).
        This is **not** the same as classification spend: classification spend
        evidence lives in ``llm_calls`` (count API-provider rows with
        role='triage_salience' or similar).  Compose/Redis/local-LLM
        availability probes are runtime checks owned by the soak gate; they
        are intentionally absent from this layer.

    now_ts format contract
    ----------------------
    ISO 8601 with UTC offset, e.g. ``2026-06-12T10:00:00+00:00`` or
    ``2026-06-12T10:00:00Z``.  The ``Z`` suffix is normalised to ``+00:00``
    internally via ``_dt()``.  All timestamps in the DB use the same
    ``+00:00``-suffixed format written by ``store._now_iso()``.
    """
    sources = store.fetch_source_health(conn)
    for item in sources.values():
        item["last_poll_age_seconds"] = _age_seconds(now_ts, item.get("last_poll_ts"))
        item["last_event_age_seconds"] = _age_seconds(now_ts, item.get("last_event_ts"))

    failed_list = fetch_failed_delivery_groups(conn)
    failed_total = count_failed_delivery_groups(conn)

    return {
        "sources": sources,
        "llm_calls": fetch_llm_role_summary(conn, now_ts=now_ts),
        "deferred_salience": fetch_deferred_summary(conn, now_ts=now_ts),
        "queue_lanes": queue_store.lane_depth(conn),
        "delivery_groups": {
            "failed": failed_list,
            "failed_total": failed_total,
            "skipped_only": fetch_skipped_only_delivery_group_count(conn),
        },
        "costs": fetch_provider_split(conn),
    }
