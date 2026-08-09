"""Persistent outbound-delivery worker.

The Secretary enqueues event alerts and morning digests. This worker owns transport attempts,
quiet-hours release, retry/backoff, lease recovery, and terminal dead-letter
state. A crash after a provider accepts a message but before SQLite records the
acknowledgement can still produce a duplicate on retry; transports do not offer
a shared exactly-once primitive, so the contract is durable at-least-once.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from tradingagents.delivery.factory import build_channel
from tradingagents.delivery.quiet_hours import next_allowed_utc
from tradingagents.delivery import queue_store
from tradingagents.persistence.db import connect


log = logging.getLogger(__name__)


def drain_one(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    channel_builder: Callable = build_channel,
    now: Optional[datetime] = None,
) -> bool:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delivery_cfg = config["delivery"]
    job = queue_store.lease_one(
        conn,
        lease_seconds=int(delivery_cfg["queue_lease_seconds"]),
        now=current,
    )
    if job is None:
        return False

    quiet_config = delivery_cfg["quiet_hours"]
    allowed = next_allowed_utc(now_utc=current, config=quiet_config)
    if allowed > current:
        queue_store.defer_for_quiet_hours(
            conn,
            delivery_job_id=job["delivery_job_id"],
            lease_token=job["lease_token"],
            quiet_hours=quiet_config,
            now=current,
        )
        return True

    delivery_id: Optional[int] = None
    try:
        brief = json.loads(job["brief_payload"])
        if not isinstance(brief, dict):
            raise ValueError("queued brief_payload is not a JSON object")
        channel = channel_builder(job["channel"], conn, config)
        if channel is None:
            queue_store.mark_failure(
                conn,
                delivery_job_id=job["delivery_job_id"],
                lease_token=job["lease_token"],
                error=f"unknown delivery channel: {job['channel']}",
                delivery_id=None,
                retryable=False,
                error_category="unknown_channel",
                now=current,
            )
            return True

        delivery_id = channel.send_attempt(
            brief=brief,
            mode=job["mode"],
            body=job["body"],
        )
        assert delivery_id is not None
        attempt = conn.execute(
            "SELECT status, skip_reason, channel_ref FROM deliveries "
            "WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if attempt is None:
            raise RuntimeError("delivery channel returned an unknown delivery id")
        if attempt["status"] == "sent":
            queue_store.mark_sent(
                conn,
                delivery_job_id=job["delivery_job_id"],
                lease_token=job["lease_token"],
                delivery_id=delivery_id,
                now=current,
            )
            return True
        if attempt["skip_reason"] == "quiet_hours":
            queue_store.defer_for_quiet_hours(
                conn,
                delivery_job_id=job["delivery_job_id"],
                lease_token=job["lease_token"],
                quiet_hours=quiet_config,
                now=current,
            )
            return True

        retryable = attempt["status"] == "failed"
        detail = attempt["channel_ref"] or attempt["skip_reason"] or attempt["status"]
        error_category = (
            "transport_error"
            if retryable
            else (attempt["skip_reason"] or "delivery_blocked")
        )
        state = queue_store.mark_failure(
            conn,
            delivery_job_id=job["delivery_job_id"],
            lease_token=job["lease_token"],
            error=str(detail),
            delivery_id=delivery_id,
            retry_base_seconds=int(delivery_cfg["queue_retry_base_seconds"]),
            retry_cap_seconds=int(delivery_cfg["queue_retry_cap_seconds"]),
            retryable=retryable,
            error_category=str(error_category),
            now=current,
        )
        log.warning(
            "delivery job %d attempt %d/%d ended state=%s: %s",
            job["delivery_job_id"],
            job["attempt_count"],
            job["max_attempts"],
            state,
            detail,
        )
    except queue_store.DeliveryLeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        state = queue_store.mark_failure(
            conn,
            delivery_job_id=job["delivery_job_id"],
            lease_token=job["lease_token"],
            error=f"{type(exc).__name__}: {exc}",
            delivery_id=delivery_id,
            retry_base_seconds=int(delivery_cfg["queue_retry_base_seconds"]),
            retry_cap_seconds=int(delivery_cfg["queue_retry_cap_seconds"]),
            retryable=True,
            error_category="worker_runtime",
            now=current,
        )
        log.exception("delivery job %d failed; state=%s", job["delivery_job_id"], state)
    return True


def main(config: Optional[dict] = None) -> None:
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    conn = connect(cfg["iic_db_path"])
    poll_seconds = max(float(cfg["delivery"].get("worker_poll_interval_s", 2)), 0.1)
    sweep_interval = max(poll_seconds, 30.0)
    last_sweep = 0.0
    log.info("delivery worker started: poll=%ss", poll_seconds)
    try:
        while True:
            monotonic_now = time.monotonic()
            if monotonic_now - last_sweep >= sweep_interval:
                recovered = queue_store.sweep_expired_leases(conn)
                if recovered:
                    log.warning("recovered %d expired delivery lease(s)", recovered)
                last_sweep = monotonic_now
            if not drain_one(conn, config=cfg):
                time.sleep(poll_seconds)
    except KeyboardInterrupt:
        log.info("delivery worker stopped")
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
