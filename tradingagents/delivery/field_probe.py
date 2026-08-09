"""Synthetic Batch 5 outbox probe for credentialed field validation."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.delivery import queue_store
from tradingagents.delivery.worker import drain_one
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


def _config(db_path: Path) -> dict:
    config = dict(DEFAULT_CONFIG)
    config["iic_db_path"] = str(db_path)
    return config


def _enqueue(db_path: Path, *, channel: str, ready_now: bool) -> None:
    conn = connect(str(db_path))
    brief_id = f"batch5-{uuid.uuid4().hex}"
    generated = datetime.now(timezone.utc).isoformat()
    store.insert_brief(
        conn,
        brief_id=brief_id,
        mode="event_alert",
        scope="BATCH5-PROBE",
        generated_ts=generated,
        content_path=f"briefs/{brief_id}.md",
        run_ids=[],
    )
    brief = {
        "brief_id": brief_id,
        "mode": "event_alert",
        "scope": "BATCH5-PROBE",
        "generated_ts": generated,
        "tickers": [
            {"ticker": "PROBE", "recommendation": "No trade; delivery test only."}
        ],
    }
    body = (
        "Batch 5 probe [PROBE] _Markdown_!"
        if channel == "telegram"
        else "<h1>Batch 5 probe</h1><p>PROBE &amp; HTML safety test.</p>"
    )
    quiet_hours = (
        {"enabled": False} if ready_now else DEFAULT_CONFIG["delivery"]["quiet_hours"]
    )
    job_id = queue_store.enqueue_alert(
        conn,
        brief_id=brief_id,
        channel=channel,
        mode="event_alert",
        brief_payload=brief,
        body=body,
        quiet_hours=quiet_hours,
        max_attempts=int(DEFAULT_CONFIG["delivery"]["queue_max_attempts"]),
    )
    job, _events = queue_store.inspect_delivery(conn, delivery_job_id=job_id)
    print(json.dumps(job, sort_keys=True))
    conn.close()


def _drain(db_path: Path) -> None:
    conn = connect(str(db_path))
    drained = drain_one(conn, config=_config(db_path))
    row = conn.execute(
        "SELECT * FROM delivery_queue ORDER BY delivery_job_id DESC LIMIT 1"
    ).fetchone()
    print(
        json.dumps(
            {"drained": drained, "latest_job": dict(row) if row else None},
            sort_keys=True,
        )
    )
    conn.close()


def _inspect(db_path: Path, *, delivery_job_id: int) -> None:
    conn = connect(str(db_path))
    job, events = queue_store.inspect_delivery(conn, delivery_job_id=delivery_job_id)
    if job is None:
        raise SystemExit(f"delivery job {delivery_job_id} does not exist")
    attempts = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM deliveries WHERE brief_id = ? ORDER BY delivery_id",
            (job["brief_id"],),
        )
    ]
    actions = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM brief_actions WHERE brief_id = ? ORDER BY action_id",
            (job["brief_id"],),
        )
    ]
    print(
        json.dumps(
            {"job": job, "events": events, "attempts": attempts, "actions": actions},
            sort_keys=True,
            indent=2,
        )
    )
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--db", required=True, type=Path)
    enqueue.add_argument("--channel", required=True, choices=("telegram", "email"))
    enqueue.add_argument(
        "--ready-now",
        action="store_true",
        help="Bypass quiet-hour scheduling for a daytime transport probe.",
    )

    drain = subparsers.add_parser("drain-one")
    drain.add_argument("--db", required=True, type=Path)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--db", required=True, type=Path)
    inspect.add_argument("--job-id", required=True, type=int)

    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "enqueue":
        _enqueue(args.db, channel=args.channel, ready_now=args.ready_now)
    elif args.command == "drain-one":
        _drain(args.db)
    else:
        _inspect(args.db, delivery_job_id=args.job_id)


if __name__ == "__main__":
    main()
