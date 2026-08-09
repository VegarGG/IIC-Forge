"""Durable, deduplicated operational alerts routed through the delivery outbox."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from tradingagents.delivery import queue_store
from tradingagents.delivery.render import render_for_channel
from tradingagents.ops.logging import redact, redact_fields
from tradingagents.persistence import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_details(details: Mapping[str, Any] | None) -> dict[str, str]:
    return redact_fields(dict(details or {}))


def record_operational_alert(
    conn: sqlite3.Connection,
    *,
    config: Mapping[str, Any],
    dedup_key: str,
    category: str,
    severity: str,
    summary: str,
    details: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> tuple[int, bool]:
    """Upsert alert state and enqueue the first occurrence for every channel.

    Returns ``(alert_id, created)``. Repeated observations increment the durable
    occurrence counter but do not spam another message.
    """
    if severity not in {"warning", "critical"}:
        raise ValueError("operational alert severity must be warning or critical")
    if not dedup_key.strip() or not category.strip() or not summary.strip():
        raise ValueError("operational alert identity and summary must not be empty")
    observed = now or _now()
    safe_summary = redact(summary).strip()[:500]
    safe_details = _safe_details(details)
    detail_json = json.dumps(safe_details, sort_keys=True, separators=(",", ":"))
    with conn:
        existing = conn.execute(
            "SELECT alert_id, state FROM operational_alerts WHERE dedup_key=?",
            (dedup_key,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE operational_alerts SET last_seen_ts=?, occurrence_count="
                "occurrence_count+1, severity=?, summary=?, details=?, state='open', "
                "resolved_ts=NULL WHERE alert_id=?",
                (
                    observed,
                    severity,
                    safe_summary,
                    detail_json,
                    existing["alert_id"],
                ),
            )
            alert_id = int(existing["alert_id"])
            if existing["state"] == "open":
                return alert_id, False
        else:
            cursor = conn.execute(
                "INSERT INTO operational_alerts (dedup_key, category, severity, "
                "summary, details, first_seen_ts, last_seen_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    dedup_key[:240],
                    category[:120],
                    severity,
                    safe_summary,
                    detail_json,
                    observed,
                    observed,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("operational alert insert returned no identifier")
            alert_id = int(cursor.lastrowid)
        brief_id = f"ops-{uuid.uuid4().hex}"
        brief = {
            "brief_id": brief_id,
            "mode": "operational_alert",
            "scope": category[:120],
            "generated_ts": observed,
            "severity": severity,
            "summary": safe_summary,
            "details": safe_details,
            "alert_id": alert_id,
        }
        store.insert_brief(
            conn,
            brief_id=brief_id,
            mode="operational_alert",
            scope=category[:120],
            generated_ts=observed,
            content_path=f"operational/{brief_id}.md",
            run_ids=[],
            commit=False,
        )
        delivery = dict(config.get("delivery") or {})
        channels = tuple(delivery.get("enabled_channels") or ())
        quiet_hours = dict(delivery.get("quiet_hours") or {})
        for channel in channels:
            if channel not in {"telegram", "email"}:
                continue
            body = render_for_channel(
                channel=channel, mode="operational_alert", brief=brief
            )
            queue_store.enqueue_alert(
                conn,
                brief_id=brief_id,
                channel=channel,
                mode="operational_alert",
                brief_payload=brief,
                body=body,
                quiet_hours=quiet_hours,
                max_attempts=int(delivery.get("queue_max_attempts", 5)),
                commit=False,
            )
        conn.execute(
            "UPDATE operational_alerts SET delivery_brief_id=?, last_delivery_ts=? "
            "WHERE alert_id=?",
            (brief_id, observed, alert_id),
        )
    return alert_id, True


def resolve_absent_alerts(
    conn: sqlite3.Connection,
    *,
    active_dedup_keys: set[str],
    operator_note: str = "condition cleared by operator monitor",
    now: str | None = None,
    managed_prefixes: tuple[str, ...] = (
        "database:", "redis:", "service:", "analysis-queue:",
        "delivery-queue:", "disk:", "backup:", "budget:",
    ),
) -> int:
    """Resolve open alerts no longer emitted by the latest complete evaluation."""
    resolved = now or _now()
    rows = list(conn.execute("SELECT alert_id, dedup_key FROM operational_alerts WHERE state='open'"))
    changed = 0
    with conn:
        for row in rows:
            key = str(row["dedup_key"])
            if not key.startswith(managed_prefixes) or key in active_dedup_keys:
                continue
            changed += conn.execute(
                "UPDATE operational_alerts SET state='resolved', resolved_ts=?, "
                "operator_note=? WHERE alert_id=? AND state='open'",
                (resolved, operator_note[:2000], row["alert_id"]),
            ).rowcount
    return changed
