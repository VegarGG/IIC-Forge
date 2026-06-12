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
