from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from tradingagents.persistence.db import connect


def _config() -> dict:
    return {
        "delivery": {"quiet_hours": {"timezone": "Asia/Shanghai"}},
        "morning_digest": {"schedule_local_time": "07:00"},
    }


@pytest.mark.unit
def test_scheduler_enqueues_once_at_beijing_boundary(tmp_path):
    from tradingagents.runtime.scheduler import (
        enqueue_due_morning_digest,
        morning_digest_brief_id,
    )

    conn = connect(str(tmp_path / "iic.db"))
    before = datetime(2026, 8, 8, 22, 59, tzinfo=timezone.utc)
    boundary = datetime(2026, 8, 8, 23, 0, tzinfo=timezone.utc)

    assert enqueue_due_morning_digest(conn, config=_config(), now=before) is None
    first = enqueue_due_morning_digest(conn, config=_config(), now=boundary)
    second = enqueue_due_morning_digest(conn, config=_config(), now=boundary)

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM queue_jobs").fetchone()
    assert row["job_type"] == "morning_digest"
    assert row["idempotency_key"] == "morning-digest:Asia/Shanghai:2026-08-09"
    payload = json.loads(row["payload"])
    assert payload == {
        "brief_id": morning_digest_brief_id(date(2026, 8, 9)),
        "local_date": "2026-08-09",
        "scheduled_ts": "2026-08-09T07:00:00+08:00",
    }


@pytest.mark.unit
def test_scheduler_enqueues_new_job_on_next_beijing_date(tmp_path):
    from tradingagents.runtime.scheduler import enqueue_due_morning_digest

    conn = connect(str(tmp_path / "iic.db"))
    enqueue_due_morning_digest(
        conn,
        config=_config(),
        now=datetime(2026, 8, 8, 23, 0, tzinfo=timezone.utc),
    )
    enqueue_due_morning_digest(
        conn,
        config=_config(),
        now=datetime(2026, 8, 9, 23, 0, tzinfo=timezone.utc),
    )
    assert conn.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0] == 2


@pytest.mark.unit
def test_scheduler_rejects_non_beijing_runtime_timezone(tmp_path):
    from tradingagents.runtime.scheduler import enqueue_due_morning_digest

    conn = connect(str(tmp_path / "iic.db"))
    config = _config()
    config["delivery"]["quiet_hours"]["timezone"] = "UTC"
    with pytest.raises(ValueError, match="Asia/Shanghai"):
        enqueue_due_morning_digest(
            conn,
            config=config,
            now=datetime(2026, 8, 8, 23, 0, tzinfo=timezone.utc),
        )
