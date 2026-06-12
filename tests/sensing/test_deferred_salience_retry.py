"""Task 6: Deferred salience retry — scheduler and runner tests.

Covers the plan's three core tests plus amendment-C extras:
  - Schedule preserves payload + backoff.
  - Runner marks done when rescored (async).
  - Runner reschedules with exponential backoff (async).
  - Dead path: persistent failures reach max_attempts → state='dead'.
  - Empty claim: run_due_retries_once with nothing due returns 0.
  - Atomic claim: claiming twice only claims once.
  - Reclaim: stale running row re-pended; fresh running row left alone.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store
from tradingagents.sensing.envelope import Envelope


# ---------------------------------------------------------------------------
# Plan test 1: schedule preserves payload + backoff
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_schedule_deferred_retry_preserves_payload_and_backoff(tmp_path):
    from tradingagents.sensing.deferred_retry import schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:1",
        text="Company reports earnings shock",
        source_tags={"tickers": ["NVDA"]},
        raw_path="/data/events/staging/rss1.json",
    )
    retry_id = schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-deferred",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=60,
    )
    row = store.fetch_deferred_salience_retries(conn)[0]
    assert row["retry_id"] == retry_id
    assert row["source"] == "rss"
    assert row["raw_path"] == "/data/events/staging/rss1.json"
    assert row["payload_hash"]
    assert row["next_attempt_ts"] == "2026-06-12T10:01:00+00:00"
    payload = json.loads(row["payload_json"])
    assert payload["external_id"] == "rss:1"
    assert payload["source_tags"] == {"tickers": ["NVDA"]}


# ---------------------------------------------------------------------------
# Plan test 2: runner marks done when rescored (async)
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_retry_runner_marks_done_when_rescored_async(tmp_path):
    from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:1",
        text="Company reports earnings shock",
        source_tags={},
        raw_path="",
    )
    retry_id = schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-deferred",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=1,
    )

    class FakeTriage:
        async def process_one(self, retry_env):
            assert retry_env.external_id == "rss:1"
            return type("Result", (), {"salience": 0.9})()

    count = await run_due_retries_once(
        conn,
        triage=FakeTriage(),
        now_ts="2026-06-12T10:00:02+00:00",
        limit=10,
        max_attempts=3,
    )
    assert count == 1
    done = store.fetch_deferred_salience_retries(conn, state="done")
    assert done[0]["retry_id"] == retry_id


# ---------------------------------------------------------------------------
# Plan test 3: runner reschedules with exponential backoff (async)
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_retry_runner_reschedules_with_exponential_backoff(tmp_path):
    from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:1",
        text="Company reports earnings shock",
        source_tags={},
        raw_path="",
    )
    schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-deferred",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=1,
    )

    class FakeTriage:
        async def process_one(self, retry_env):
            return type("Result", (), {"salience": None})()

    count = await run_due_retries_once(
        conn,
        triage=FakeTriage(),
        now_ts="2026-06-12T10:00:02+00:00",
        limit=10,
        max_attempts=3,
    )
    assert count == 1
    pending = store.fetch_deferred_salience_retries(conn, state="pending")
    assert pending[0]["attempt_count"] == 1
    assert pending[0]["next_attempt_ts"] == "2026-06-12T10:02:02+00:00"


# ---------------------------------------------------------------------------
# Amendment C test 1: dead path — max_attempts reached → state='dead'
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_retry_reaches_max_attempts_becomes_dead(tmp_path):
    from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:dead",
        text="Perpetually deferred event",
        source_tags={},
        raw_path="",
    )
    retry_id = schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-dead",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=1,
    )

    class FakeTriage:
        async def process_one(self, retry_env):
            return type("Result", (), {"salience": None})()

    # First two attempts: reschedule (attempt_count < max_attempts=3 after claim)
    for i in range(1, 3):
        # Need to claim when row is due; keep now_ts ahead of next_attempt_ts
        rows = store.fetch_deferred_salience_retries(conn, state="pending")
        assert rows, f"Expected pending row before attempt {i+1}"
        # Advance now past the scheduled next_attempt_ts
        advance_ts = rows[0]["next_attempt_ts"]
        # Add 1 second to advance past it
        advance_dt = datetime.fromisoformat(advance_ts) + timedelta(seconds=1)
        advance_ts_str = advance_dt.isoformat()
        count = await run_due_retries_once(
            conn,
            triage=FakeTriage(),
            now_ts=advance_ts_str,
            limit=10,
            max_attempts=3,
        )
        assert count == 1

    # Third attempt: attempt_count will be 3 after claim, equals max_attempts → dead
    rows = store.fetch_deferred_salience_retries(conn, state="pending")
    assert rows, "Expected pending row for final attempt"
    advance_dt = datetime.fromisoformat(rows[0]["next_attempt_ts"]) + timedelta(seconds=1)
    count = await run_due_retries_once(
        conn,
        triage=FakeTriage(),
        now_ts=advance_dt.isoformat(),
        limit=10,
        max_attempts=3,
    )
    assert count == 1
    dead = store.fetch_deferred_salience_retries(conn, state="dead")
    assert dead[0]["retry_id"] == retry_id
    assert dead[0]["last_error"]  # reason recorded


# ---------------------------------------------------------------------------
# Amendment C test 2: empty claim — nothing due returns 0
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_run_due_retries_with_nothing_due_returns_zero(tmp_path):
    from tradingagents.sensing.deferred_retry import run_due_retries_once, schedule_deferred_retry

    conn = connect(str(tmp_path / "iic.db"))
    env = Envelope(
        source="rss",
        ingested_ts="2026-06-12T10:00:00+00:00",
        external_id="rss:future",
        text="Future event",
        source_tags={},
        raw_path="",
    )
    schedule_deferred_retry(
        conn,
        env=env,
        event_id="ev-future",
        reason="llm_error",
        now_ts="2026-06-12T10:00:00+00:00",
        base_delay_seconds=3600,  # 1 hour from now
    )

    class FakeTriage:
        async def process_one(self, retry_env):
            raise AssertionError("Should not be called")

    count = await run_due_retries_once(
        conn,
        triage=FakeTriage(),
        now_ts="2026-06-12T10:00:30+00:00",  # well before next_attempt_ts
        limit=10,
        max_attempts=3,
    )
    assert count == 0
    # Row still pending, untouched
    pending = store.fetch_deferred_salience_retries(conn, state="pending")
    assert len(pending) == 1


# ---------------------------------------------------------------------------
# Amendment C test 3: atomic claim — second claim returns empty
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_claim_twice_only_claims_once(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    store.insert_deferred_salience_retry(
        conn,
        event_id="ev-once",
        source="rss",
        raw_path="",
        payload_hash="abc",
        payload_json='{"source":"rss","ingested_ts":"2026-06-12T10:00:00+00:00","external_id":"x","text":"t","source_tags":{},"raw_path":""}',
        reason="llm_error",
        next_attempt_ts="2026-06-12T10:01:00+00:00",
    )

    now_ts = "2026-06-12T10:02:00+00:00"
    first = store.claim_due_deferred_salience_retries(conn, now_ts=now_ts, limit=5)
    assert len(first) == 1

    second = store.claim_due_deferred_salience_retries(conn, now_ts=now_ts, limit=5)
    assert second == []


# ---------------------------------------------------------------------------
# Amendment C test 4: reclaim stale running row; fresh running row untouched
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_reclaim_stale_running_row(tmp_path):
    from tradingagents.sensing.deferred_retry import RECLAIM_RUNNING_AFTER_SECONDS, reclaim_stale_running

    conn = connect(str(tmp_path / "iic.db"))

    # Insert two rows and claim them both (transitions to running).
    stale_next_ts = "2026-06-12T09:00:00+00:00"
    fresh_next_ts = "2026-06-12T10:00:00+00:00"

    store.insert_deferred_salience_retry(
        conn,
        event_id="ev-stale",
        source="rss",
        raw_path="",
        payload_hash="stale-hash",
        payload_json='{"source":"rss","ingested_ts":"2026-06-12T09:00:00+00:00","external_id":"stale","text":"stale","source_tags":{},"raw_path":""}',
        reason="llm_error",
        next_attempt_ts=stale_next_ts,
    )
    store.insert_deferred_salience_retry(
        conn,
        event_id="ev-fresh",
        source="rss",
        raw_path="",
        payload_hash="fresh-hash",
        payload_json='{"source":"rss","ingested_ts":"2026-06-12T10:00:00+00:00","external_id":"fresh","text":"fresh","source_tags":{},"raw_path":""}',
        reason="llm_error",
        next_attempt_ts=fresh_next_ts,
    )

    # Claim both rows at the same now_ts (both become running with updated_ts = now_ts).
    claim_ts = "2026-06-12T10:01:00+00:00"
    claimed = store.claim_due_deferred_salience_retries(conn, now_ts=claim_ts, limit=10)
    assert len(claimed) == 2

    # Manually backdate the stale row's updated_ts to simulate a dead claimer.
    stale_row = [r for r in claimed if r["event_id"] == "ev-stale"][0]
    stale_id = stale_row["retry_id"]
    old_ts = "2026-06-12T09:30:00+00:00"  # 90 minutes before reclaim_ts
    conn.execute(
        "UPDATE deferred_salience_retry SET updated_ts=? WHERE retry_id=?",
        (old_ts, stale_id),
    )
    conn.commit()

    # Reclaim at a "now" that is RECLAIM_RUNNING_AFTER_SECONDS after old_ts+1s.
    # old_ts = 09:30; adding RECLAIM_RUNNING_AFTER_SECONDS (1800s=30min) → 10:00+1s.
    # Use 10:02 to be safely past the cutoff.
    reclaim_now_ts = "2026-06-12T10:02:00+00:00"
    reclaimed = reclaim_stale_running(conn, reclaim_now_ts)
    assert reclaimed == 1  # only the stale row

    # Stale row should now be pending again.
    pending = store.fetch_deferred_salience_retries(conn, state="pending")
    assert len(pending) == 1
    assert pending[0]["retry_id"] == stale_id

    # Fresh row should still be running.
    running = store.fetch_deferred_salience_retries(conn, state="running")
    assert len(running) == 1
    fresh_row = [r for r in claimed if r["event_id"] == "ev-fresh"][0]
    assert running[0]["retry_id"] == fresh_row["retry_id"]
