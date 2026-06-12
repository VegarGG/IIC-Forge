"""Tests for the shared operational query layer (operations.py panel)."""

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_brief(conn, brief_id="b1"):
    store.insert_brief(
        conn,
        brief_id=brief_id,
        mode="event_alert_light",
        scope='["NVDA"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path=f"briefs/{brief_id}.md",
        run_ids=[],
    )


def _insert_event(conn, event_id, *, salience=None, salience_source=None):
    conn.execute(
        "INSERT INTO events (event_id, source, ingested_ts, salience, salience_source, status) "
        "VALUES (?, 'gdelt', '2026-06-12T09:00:00+00:00', ?, ?, 'new')",
        (event_id, salience, salience_source),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Plan baseline test (per Task 10 plan)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_operations_snapshot_reads_shared_evidence(tmp_path):
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    conn = connect(str(tmp_path / "iic.db"))
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T10:00:00+00:00",
        last_success_ts="2026-06-12T10:00:00+00:00",
        last_event_ts="2026-06-12T10:00:00+00:00",
        cursor="c1",
        cursor_updated_ts="2026-06-12T10:00:00+00:00",
        events_emitted_last_poll=1,
        diagnostics={},
    )
    store.insert_llm_call(
        conn,
        created_ts="2026-06-12T10:00:00+00:00",
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen",
        base_url="http://local",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        status="success",
        latency_ms=50,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=0.0,
        error_class=None,
        error_message=None,
    )
    snap = fetch_operations_snapshot(conn, now_ts="2026-06-12T10:05:00+00:00")
    assert snap["sources"]["gdelt"]["consecutive_failures"] == 0
    assert snap["llm_calls"]["triage_salience"]["total"] == 1
    assert snap["llm_calls"]["triage_salience"]["parse_failures"] == 0
    assert "deferred_salience" in snap
    assert "delivery_groups" in snap


# ---------------------------------------------------------------------------
# Amendment A: honest failed-group semantics
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_quiet_hours_only_group_not_in_failed(tmp_path):
    """A group where all deliveries are 'skipped' (policy skip) must NOT appear
    in failed delivery groups — it should count in skipped_only instead."""
    from tradingagents.dashboard.panels.operations import (
        fetch_failed_delivery_groups,
        fetch_skipped_only_delivery_group_count,
    )

    conn = connect(str(tmp_path / "iic.db"))
    _insert_brief(conn, "b1")

    # Group g1: telegram skipped + email suppressed, no failed attempts
    store.insert_delivery(
        conn,
        brief_id="b1",
        channel="telegram",
        status="skipped",
        sent_ts=None,
        channel_ref=None,
        skip_reason="quiet_hours",
        delivery_group_id="g1",
        attempt_rank=1,
    )
    store.insert_delivery(
        conn,
        brief_id="b1",
        channel="email",
        status="skipped",
        sent_ts=None,
        channel_ref=None,
        skip_reason="suppressed",
        delivery_group_id="g1",
        attempt_rank=2,
    )

    failed = fetch_failed_delivery_groups(conn)
    skipped_only = fetch_skipped_only_delivery_group_count(conn)

    assert failed == [], "quiet-hours-only group must NOT appear in failed"
    assert skipped_only == 1, "quiet-hours-only group must appear in skipped_only"


@pytest.mark.unit
def test_genuinely_failed_group_in_failed(tmp_path):
    """A group with at least one 'failed' attempt and no 'sent' must appear
    in failed delivery groups."""
    from tradingagents.dashboard.panels.operations import (
        fetch_failed_delivery_groups,
        fetch_skipped_only_delivery_group_count,
    )

    conn = connect(str(tmp_path / "iic.db"))
    _insert_brief(conn, "b1")

    # Group g2: telegram failed, email skipped → genuinely failed
    store.insert_delivery(
        conn,
        brief_id="b1",
        channel="telegram",
        status="failed",
        sent_ts=None,
        channel_ref=None,
        skip_reason=None,
        delivery_group_id="g2",
        attempt_rank=1,
        failure_reason="connection_refused",
    )
    store.insert_delivery(
        conn,
        brief_id="b1",
        channel="email",
        status="skipped",
        sent_ts=None,
        channel_ref=None,
        skip_reason="suppressed",
        delivery_group_id="g2",
        attempt_rank=2,
    )

    failed = fetch_failed_delivery_groups(conn)
    skipped_only = fetch_skipped_only_delivery_group_count(conn)

    assert len(failed) == 1
    assert failed[0]["delivery_group_id"] == "g2"
    assert skipped_only == 0, "no skipped-only groups — this one had a failure"


@pytest.mark.unit
def test_sent_group_not_in_failed_or_skipped(tmp_path):
    """A group with at least one 'sent' attempt must appear in neither failed
    nor skipped_only."""
    from tradingagents.dashboard.panels.operations import (
        fetch_failed_delivery_groups,
        fetch_skipped_only_delivery_group_count,
    )

    conn = connect(str(tmp_path / "iic.db"))
    _insert_brief(conn, "b1")

    store.insert_delivery(
        conn,
        brief_id="b1",
        channel="telegram",
        status="sent",
        sent_ts="2026-06-12T10:00:00+00:00",
        channel_ref="tg:123",
        skip_reason=None,
        delivery_group_id="g3",
        attempt_rank=1,
    )

    failed = fetch_failed_delivery_groups(conn)
    skipped_only = fetch_skipped_only_delivery_group_count(conn)

    assert failed == []
    assert skipped_only == 0


@pytest.mark.unit
def test_delivery_groups_key_shape_in_snapshot(tmp_path):
    """fetch_operations_snapshot returns delivery_groups with both 'failed' list
    and 'skipped_only' int keys."""
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    conn = connect(str(tmp_path / "iic.db"))
    snap = fetch_operations_snapshot(conn, now_ts="2026-06-12T10:00:00+00:00")

    assert "delivery_groups" in snap
    dg = snap["delivery_groups"]
    assert "failed" in dg
    assert isinstance(dg["failed"], list)
    assert "skipped_only" in dg
    assert isinstance(dg["skipped_only"], int)


# ---------------------------------------------------------------------------
# Amendment B: orphaned deferred events
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_orphaned_events_count_counted(tmp_path):
    """An event with salience_source='deferred', salience=NULL, and no retry
    row at all counts as an orphaned event."""
    from tradingagents.dashboard.panels.operations import fetch_deferred_summary

    conn = connect(str(tmp_path / "iic.db"))
    _insert_event(conn, "ev-orphan", salience=None, salience_source="deferred")

    summary = fetch_deferred_summary(conn)
    assert summary["orphaned_events"] == 1


@pytest.mark.unit
def test_recovered_event_not_counted_as_orphan(tmp_path):
    """An event that has a retry row in state 'done' must NOT count as orphaned,
    even though its original salience column remains NULL."""
    from tradingagents.dashboard.panels.operations import fetch_deferred_summary

    conn = connect(str(tmp_path / "iic.db"))
    _insert_event(conn, "ev-recovered", salience=None, salience_source="deferred")

    # Insert a 'done' retry row for the recovered event
    store.insert_deferred_salience_retry(
        conn,
        event_id="ev-recovered",
        source="gdelt",
        raw_path=None,
        payload_hash="abc123",
        payload_json='{"text": "news"}',
        reason="local_unavailable",
        next_attempt_ts="2026-06-12T09:00:00+00:00",
    )
    # Manually mark it done (store doesn't expose a done-transition helper here)
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'done' WHERE event_id = 'ev-recovered'"
    )
    conn.commit()

    summary = fetch_deferred_summary(conn)
    assert summary["orphaned_events"] == 0, "done retry row means event is recovered"


@pytest.mark.unit
def test_active_retry_event_not_counted_as_orphan(tmp_path):
    """An event with an active pending retry row must NOT count as orphaned."""
    from tradingagents.dashboard.panels.operations import fetch_deferred_summary

    conn = connect(str(tmp_path / "iic.db"))
    _insert_event(conn, "ev-pending", salience=None, salience_source="deferred")

    store.insert_deferred_salience_retry(
        conn,
        event_id="ev-pending",
        source="gdelt",
        raw_path=None,
        payload_hash="def456",
        payload_json='{"text": "news2"}',
        reason="local_unavailable",
        next_attempt_ts="2026-06-12T11:00:00+00:00",
    )
    # State is 'pending' by default

    summary = fetch_deferred_summary(conn)
    assert summary["orphaned_events"] == 0, "pending retry row — not an orphan"


@pytest.mark.unit
def test_deferred_summary_in_snapshot(tmp_path):
    """fetch_operations_snapshot includes deferred_salience with orphaned_events key."""
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    conn = connect(str(tmp_path / "iic.db"))
    _insert_event(conn, "ev-orphan2", salience=None, salience_source="deferred")

    snap = fetch_operations_snapshot(conn, now_ts="2026-06-12T10:00:00+00:00")
    assert "deferred_salience" in snap
    assert "orphaned_events" in snap["deferred_salience"]
    assert snap["deferred_salience"]["orphaned_events"] == 1
