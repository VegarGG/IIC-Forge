"""Ordered delivery policy: Telegram primary, email fallback."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class DeliveryPolicyResult:
    delivery_group_id: str
    final_status: str
    attempt_ids: list[int]


def deliver_ordered(
    *,
    conn: sqlite3.Connection,
    brief: dict[str, Any],
    mode: str,
    bodies: dict[str, str],
    channels: dict[str, Any],
    urgent: bool = False,
) -> DeliveryPolicyResult:
    """Ordered-delivery policy: Telegram primary (rank 1), email fallback (rank 2).

    Ordering contract:
      - Telegram is always attempted first (attempt_rank=1).  If it succeeds
        (status="sent"), the function returns immediately and email is never
        attempted.
      - If Telegram fails or is otherwise unavailable (status != "sent" AND
        skip_reason != "quiet_hours"), email is attempted as a fallback
        (attempt_rank=2, is_fallback=True, fallback_of=<telegram delivery_id>).

    Quiet-hours short-circuit:
      - When Telegram is skipped with skip_reason="quiet_hours" AND
        urgent=False, the function returns immediately without attempting
        email.  This is the only case where a skipped Telegram prevents
        fallthrough to email.

    urgent flag:
      - urgent=True pierces the quiet-hours short-circuit: a quiet-hours skip
        on Telegram will fall through to email even during quiet hours.
      - No production caller currently sets urgent=True.  The parameter exists
        as a hook for a future brief-urgency model (design §9.4: "unless the
        brief is marked urgent").  Briefs cannot be marked urgent yet, so
        urgent=False everywhere is the faithful current state.

    DeliveryPolicyResult.final_status:
      - Reflects the status of the last attempted delivery row ("sent",
        "skipped", or "failed").  Recorded in the returned result for future
        gate use (e.g. suppression logic or audit queries).  Current callers
        discard it.
    """
    group_id = uuid.uuid4().hex
    attempt_ids: list[int] = []

    telegram = channels.get("telegram")
    email = channels.get("email")

    if telegram is None and email is None:
        return DeliveryPolicyResult(group_id, "skipped", [])

    primary_id = None
    primary_status = "skipped"
    if telegram is not None:
        primary_id = telegram.send(
            brief=brief,
            mode=mode,
            body=bodies.get("telegram", ""),
            delivery_group_id=group_id,
            attempt_rank=1,
            fallback_of=None,
            is_fallback=False,
        )
        attempt_ids.append(primary_id)
        row = conn.execute(
            "SELECT status, skip_reason FROM deliveries WHERE delivery_id = ?",
            (primary_id,),
        ).fetchone()
        primary_status = row["status"]
        if primary_status == "sent":
            return DeliveryPolicyResult(group_id, "sent", attempt_ids)
        if primary_status == "skipped" and row["skip_reason"] == "quiet_hours" and not urgent:
            return DeliveryPolicyResult(group_id, "skipped", attempt_ids)

    if email is None:
        return DeliveryPolicyResult(group_id, primary_status, attempt_ids)

    fallback_id = email.send(
        brief=brief,
        mode=mode,
        body=bodies.get("email", ""),
        delivery_group_id=group_id,
        attempt_rank=2,
        fallback_of=primary_id,
        is_fallback=True,
    )
    attempt_ids.append(fallback_id)
    row = conn.execute(
        "SELECT status FROM deliveries WHERE delivery_id = ?",
        (fallback_id,),
    ).fetchone()
    return DeliveryPolicyResult(group_id, row["status"], attempt_ids)
