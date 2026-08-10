#!/usr/bin/env python3
"""Credential-free budget and Beijing quiet-hours release probes."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.delivery import queue_store
from tradingagents.llm_clients.daily_budget import DailyBudgetExceeded, DailyUsdBudget
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


QUIET_HOURS = {
    "enabled": True,
    "start": "22:00",
    "end": "07:00",
    "timezone": "Asia/Shanghai",
}


def budget_probe(db_path: Path) -> dict[str, object]:
    if db_path.exists():
        raise ValueError(f"refusing to use existing probe database: {db_path}")
    conn = connect(str(db_path))
    conn.close()
    budget = DailyUsdBudget(
        db_path=str(db_path),
        provider="deepseek",
        model="deepseek-v4-pro",
        daily_limit_usd=20,
        reservation_usd=1,
        timezone_name="Asia/Shanghai",
    )
    for index in range(20):
        budget.reserve(f"batch10-{index}")
    blocked = False
    try:
        budget.reserve("batch10-over-limit")
    except DailyBudgetExceeded:
        blocked = True
    conn = connect(str(db_path))
    total = float(
        conn.execute(
            "SELECT COALESCE(SUM(reserved_usd), 0) FROM llm_budget_ledger"
        ).fetchone()[0]
    )
    conn.close()
    if not blocked or total != 20:
        raise AssertionError(f"budget probe failed: blocked={blocked}, total={total}")
    return {"status": "passed", "limit_usd": 20, "reserved_usd": total, "next_call_blocked": blocked}


def quiet_hours_probe(db_path: Path) -> dict[str, object]:
    if db_path.exists():
        raise ValueError(f"refusing to use existing probe database: {db_path}")
    conn = connect(str(db_path))
    brief_id = "batch10-quiet-hours"
    created = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)
    store.insert_brief(
        conn,
        brief_id=brief_id,
        mode="operational_alert",
        scope="batch10",
        generated_ts=created.isoformat(),
        content_path="briefs/batch10-quiet-hours.md",
        run_ids=[],
    )
    job_id = queue_store.enqueue_alert(
        conn,
        brief_id=brief_id,
        channel="telegram",
        mode="operational_alert",
        brief_payload={"brief_id": brief_id, "mode": "operational_alert"},
        body="Batch 10 quiet-hours probe",
        quiet_hours=QUIET_HOURS,
        now=created,
    )
    row = conn.execute(
        "SELECT state, available_ts FROM delivery_queue WHERE delivery_job_id=?",
        (job_id,),
    ).fetchone()
    conn.close()
    expected = datetime(2026, 8, 9, 23, 0, tzinfo=timezone.utc).isoformat()
    if row is None or row["state"] != "queued" or row["available_ts"] != expected:
        raise AssertionError(f"quiet-hours probe failed: {dict(row) if row else None}")
    return {
        "status": "passed",
        "timezone": "Asia/Shanghai",
        "created_utc": created.isoformat(),
        "available_utc": row["available_ts"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("budget", "quiet-hours"))
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    result = budget_probe(args.db) if args.mode == "budget" else quiet_hours_probe(args.db)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
