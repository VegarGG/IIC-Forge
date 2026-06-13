import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


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


@pytest.mark.unit
def test_lease_sets_heartbeat_ts(conn):
    """lease_one sets heartbeat_ts to the same value as started_ts on claim.

    Periodic in-flight heartbeat updates are future work; the sweep uses
    started_ts + timeout_seconds.
    """
    from tradingagents.orchestrator import queue_store

    queue_store.insert_queue_job(
        conn, job_type="run_full_study", payload="{}", trigger_event_id=None,
        lane="deep",
    )
    job = queue_store.lease_one(conn, lane="deep")
    assert job is not None

    row = conn.execute(
        "SELECT started_ts, heartbeat_ts FROM queue_jobs WHERE job_id = ?",
        (job["job_id"],),
    ).fetchone()
    assert row["heartbeat_ts"] is not None
    assert row["heartbeat_ts"] == row["started_ts"]


@pytest.mark.unit
def test_sweep_respects_per_job_timeout(conn):
    """A running job whose per-job timeout_seconds is exceeded is swept;
    one with a long per-job timeout is left alone, even if the global
    fallback would sweep it.
    """
    from tradingagents.orchestrator import queue_store

    # Insert two jobs with different per-job timeouts.
    j1 = queue_store.insert_queue_job(
        conn, job_type="run_full_study", payload="{}", trigger_event_id=None,
        lane="deep", timeout_seconds=60,
    )
    j2 = queue_store.insert_queue_job(
        conn, job_type="run_full_study", payload="{}", trigger_event_id=None,
        lane="deep", timeout_seconds=3600,
    )
    # Lease both to put them in 'running' state.
    queue_store.lease_one(conn, lane="deep")
    queue_store.lease_one(conn, lane="deep")

    # Back-date started_ts to 120 seconds ago for both.
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    assert "T" in stale_ts  # Confirm ISO-8601 with T separator.
    conn.execute("UPDATE queue_jobs SET started_ts = ?", (stale_ts,))
    conn.commit()

    # Global fallback max_age is 7200 — large enough that it would NOT sweep
    # either job on its own. Only the per-job timeout governs the decision.
    n = queue_store.sweep_stale_leases(conn, max_age_seconds=7200)
    assert n == 1

    r1 = conn.execute(
        "SELECT state FROM queue_jobs WHERE job_id = ?", (j1,)
    ).fetchone()
    r2 = conn.execute(
        "SELECT state FROM queue_jobs WHERE job_id = ?", (j2,)
    ).fetchone()
    assert r1["state"] == "error", "job with timeout=60 should have been swept"
    assert r2["state"] == "running", "job with timeout=3600 should NOT be swept"


@pytest.mark.unit
def test_enqueue_full_study_sets_lane_and_timeout(tmp_path):
    """Driving the action_handler enqueue path sets lane='deep' and
    timeout_seconds=1200 (the DEFAULT_CONFIG deep-lane timeout).
    """
    from tradingagents.orchestrator.action_handler import tick

    conn = connect(str(tmp_path / "iic.db"))
    store.insert_event(conn, event_id="ev1", source="rss",
                       ingested_ts="2026-06-01T00:00:00+00:00", salience=0.9,
                       raw_path=None, status="triaged", deduped_of=None)
    store.insert_brief(conn, brief_id="lb1", mode="event_alert_light",
                       scope='["NVDA"]', generated_ts="2026-06-01T00:00:00+00:00",
                       content_path="briefs/lb1.md", run_ids=[],
                       trigger_event_id="ev1")
    aid = store.insert_brief_action(conn, brief_id="lb1",
                                    action_type="run_full_study",
                                    action_params={"ticker": "NVDA"},
                                    expires_at="2099-01-01T00:00:00+00:00")
    store.update_action_state(conn, action_id=aid, state="accepted",
                              responded_at="2026-06-01T01:00:00+00:00")

    tick(conn=conn, secretary=MagicMock(), dispatch_backtest=MagicMock())

    row = conn.execute(
        "SELECT lane, timeout_seconds FROM queue_jobs"
    ).fetchone()
    assert row is not None, "no job was enqueued"
    assert row["lane"] == "deep"
    assert row["timeout_seconds"] == 1200
