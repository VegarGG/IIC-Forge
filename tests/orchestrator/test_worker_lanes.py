import json

import pytest

from tradingagents.persistence.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "iic.db"))


@pytest.mark.unit
def test_insert_queue_job_sets_lane(conn):
    from tradingagents.orchestrator import queue_store

    job_id = queue_store.insert_queue_job(
        conn,
        job_type="run_full_study",
        payload=json.dumps({"ticker": "NVDA"}),
        trigger_event_id=None,
        lane="deep",
        timeout_seconds=1200,
    )
    row = conn.execute("SELECT lane, timeout_seconds FROM queue_jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert row["lane"] == "deep"
    assert row["timeout_seconds"] == 1200


@pytest.mark.unit
def test_lease_one_only_claims_matching_lane(conn):
    from tradingagents.orchestrator import queue_store

    queue_store.insert_queue_job(conn, job_type="refine_brief", payload="{}", trigger_event_id=None, lane="action")
    queue_store.insert_queue_job(conn, job_type="run_full_study", payload="{}", trigger_event_id=None, lane="deep")

    deep = queue_store.lease_one(conn, lane="deep")
    assert deep["job_type"] == "run_full_study"
    action = queue_store.lease_one(conn, lane="action")
    assert action["job_type"] == "refine_brief"


@pytest.mark.unit
def test_queue_lane_depth(conn):
    from tradingagents.orchestrator import queue_store

    queue_store.insert_queue_job(conn, job_type="refine_brief", payload="{}", trigger_event_id=None, lane="action")
    queue_store.insert_queue_job(conn, job_type="run_full_study", payload="{}", trigger_event_id=None, lane="deep")
    assert queue_store.lane_depth(conn) == {
        "action": {"queued": 1},
        "deep": {"queued": 1},
    }
