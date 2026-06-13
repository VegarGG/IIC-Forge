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


def _insert_llm_call(conn, created_ts, *, role="triage_salience", status="success"):
    store.insert_llm_call(
        conn,
        created_ts=created_ts,
        role=role,
        service_name="triage",
        provider="local",
        model_id="qwen",
        base_url="http://local",
        request_kind="structured",
        linked_type="event",
        linked_id="ev1",
        status=status,
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
    _insert_llm_call(conn, "2026-06-12T10:00:00+00:00")
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
    """fetch_operations_snapshot returns delivery_groups with 'failed' list,
    'failed_total' int, and 'skipped_only' int keys."""
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    conn = connect(str(tmp_path / "iic.db"))
    snap = fetch_operations_snapshot(conn, now_ts="2026-06-12T10:00:00+00:00")

    assert "delivery_groups" in snap
    dg = snap["delivery_groups"]
    assert "failed" in dg
    assert isinstance(dg["failed"], list)
    assert "failed_total" in dg
    assert isinstance(dg["failed_total"], int)
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


# ---------------------------------------------------------------------------
# New tests: LLM call windowing
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_llm_summary_window_excludes_old_calls(tmp_path):
    """An llm_call older than the window is excluded when now_ts is given."""
    from tradingagents.dashboard.panels.operations import fetch_llm_role_summary

    conn = connect(str(tmp_path / "iic.db"))
    # Old call: 25 hours before now_ts
    _insert_llm_call(conn, "2026-06-11T09:00:00+00:00", role="triage_salience")
    # Recent call: 1 hour before now_ts
    _insert_llm_call(conn, "2026-06-12T09:00:00+00:00", role="triage_salience")

    now_ts = "2026-06-12T10:00:00+00:00"
    # With window (24h): only recent call should appear
    summary = fetch_llm_role_summary(conn, now_ts=now_ts, window_seconds=86400)
    assert "triage_salience" in summary
    assert summary["triage_salience"]["total"] == 1, (
        "only the recent call (within 24h window) should be counted"
    )


@pytest.mark.unit
def test_llm_summary_no_now_ts_includes_all_calls(tmp_path):
    """Without now_ts, all calls are included (all-time, backward-compatible)."""
    from tradingagents.dashboard.panels.operations import fetch_llm_role_summary

    conn = connect(str(tmp_path / "iic.db"))
    # Old call: 25 hours before a hypothetical now
    _insert_llm_call(conn, "2026-06-11T09:00:00+00:00", role="triage_salience")
    # Recent call
    _insert_llm_call(conn, "2026-06-12T09:00:00+00:00", role="triage_salience")

    # No now_ts → all-time
    summary = fetch_llm_role_summary(conn)
    assert "triage_salience" in summary
    assert summary["triage_salience"]["total"] == 2, (
        "all-time query must include both calls"
    )


# ---------------------------------------------------------------------------
# New tests: oldest_pending_age_seconds
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_oldest_pending_age_seconds_with_pending_row(tmp_path):
    """When a pending retry row exists and now_ts is given, oldest_pending_age_seconds
    should be a positive float."""
    from tradingagents.dashboard.panels.operations import fetch_deferred_summary

    conn = connect(str(tmp_path / "iic.db"))
    _insert_event(conn, "ev-age-test", salience=None, salience_source="deferred")

    # Insert directly so we can control created_ts (store function always uses _now_iso)
    conn.execute(
        "INSERT INTO deferred_salience_retry "
        "(event_id, source, raw_path, payload_hash, payload_json, reason, "
        "next_attempt_ts, state, last_error, created_ts, updated_ts) "
        "VALUES (?, 'gdelt', NULL, 'agehash1', '{\"text\":\"age test\"}', "
        "'local_unavailable', '2026-06-12T11:00:00+00:00', 'pending', "
        "'local_unavailable', '2026-06-12T08:00:00+00:00', '2026-06-12T08:00:00+00:00')",
        ("ev-age-test",),
    )
    conn.commit()

    now_ts = "2026-06-12T10:00:00+00:00"
    summary = fetch_deferred_summary(conn, now_ts=now_ts)
    age = summary["oldest_pending_age_seconds"]
    assert age is not None
    assert age > 0, "pending row created 2h ago should have positive age"


@pytest.mark.unit
def test_oldest_pending_age_seconds_no_pending(tmp_path):
    """When no pending rows exist, oldest_pending_age_seconds is None."""
    from tradingagents.dashboard.panels.operations import fetch_deferred_summary

    conn = connect(str(tmp_path / "iic.db"))
    # No deferred retry rows at all
    now_ts = "2026-06-12T10:00:00+00:00"
    summary = fetch_deferred_summary(conn, now_ts=now_ts)
    assert summary["oldest_pending_age_seconds"] is None


@pytest.mark.unit
def test_oldest_pending_age_seconds_no_now_ts(tmp_path):
    """Without now_ts, oldest_pending_age_seconds is None even if pending rows exist."""
    from tradingagents.dashboard.panels.operations import fetch_deferred_summary

    conn = connect(str(tmp_path / "iic.db"))
    _insert_event(conn, "ev-age-no-ts", salience=None, salience_source="deferred")

    # Insert directly so we can control created_ts (store function always uses _now_iso)
    conn.execute(
        "INSERT INTO deferred_salience_retry "
        "(event_id, source, raw_path, payload_hash, payload_json, reason, "
        "next_attempt_ts, state, last_error, created_ts, updated_ts) "
        "VALUES (?, 'gdelt', NULL, 'agehash2', '{\"text\":\"no ts test\"}', "
        "'local_unavailable', '2026-06-12T11:00:00+00:00', 'pending', "
        "'local_unavailable', '2026-06-12T08:00:00+00:00', '2026-06-12T08:00:00+00:00')",
        ("ev-age-no-ts",),
    )
    conn.commit()

    summary = fetch_deferred_summary(conn)
    assert summary["oldest_pending_age_seconds"] is None


# ---------------------------------------------------------------------------
# New tests: failed_total / capped list shape
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_failed_total_matches_count(tmp_path):
    """count_failed_delivery_groups returns the same total as len(fetch_failed_delivery_groups
    with a large limit), and failed_total in the snapshot matches."""
    from tradingagents.dashboard.panels.operations import (
        count_failed_delivery_groups,
        fetch_failed_delivery_groups,
        fetch_operations_snapshot,
    )

    conn = connect(str(tmp_path / "iic.db"))
    # Insert 3 separate failed groups
    for i in range(1, 4):
        _insert_brief(conn, f"b{i}")
        store.insert_delivery(
            conn,
            brief_id=f"b{i}",
            channel="telegram",
            status="failed",
            sent_ts=None,
            channel_ref=None,
            skip_reason=None,
            delivery_group_id=f"gfail{i}",
            attempt_rank=1,
            failure_reason="connection_refused",
        )

    total = count_failed_delivery_groups(conn)
    capped = fetch_failed_delivery_groups(conn, limit=50)
    assert total == 3
    assert len(capped) == 3

    snap = fetch_operations_snapshot(conn, now_ts="2026-06-12T10:00:00+00:00")
    assert snap["delivery_groups"]["failed_total"] == 3
    assert len(snap["delivery_groups"]["failed"]) == 3


@pytest.mark.unit
def test_count_api_classification_calls_counts_api_and_respects_window(tmp_path):
    """count_api_classification_calls (from operations module):
    - counts triage_salience rows with non-local provider
    - excludes local-provider rows
    - respects the window (excludes rows older than window_seconds)
    """
    from tradingagents.dashboard.panels.operations import count_api_classification_calls

    conn = connect(str(tmp_path / "iic.db"))
    now_ts = "2026-06-12T10:00:00+00:00"

    # API-provider triage_salience call (should be counted)
    store.insert_llm_call(
        conn,
        created_ts=now_ts,
        role="triage_salience",
        service_name="triage",
        provider="openai",
        model_id="gpt-4o",
        base_url=None,
        request_kind="structured",
        linked_type="event",
        linked_id="ev_api",
        status="success",
        latency_ms=200,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=None,
        error_class=None,
        error_message=None,
    )
    # Local-provider triage_salience call (should NOT be counted)
    store.insert_llm_call(
        conn,
        created_ts=now_ts,
        role="triage_salience",
        service_name="triage",
        provider="local",
        model_id="qwen",
        base_url="http://local",
        request_kind="structured",
        linked_type="event",
        linked_id="ev_local",
        status="success",
        latency_ms=40,
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
    # API-provider triage_salience call from 25h ago (outside 24h window)
    old_ts = "2026-06-11T09:00:00+00:00"
    store.insert_llm_call(
        conn,
        created_ts=old_ts,
        role="triage_salience",
        service_name="triage",
        provider="deepseek",
        model_id="deepseek-chat",
        base_url=None,
        request_kind="structured",
        linked_type="event",
        linked_id="ev_old",
        status="success",
        latency_ms=300,
        parse_ok=True,
        fallback_mode="none",
        fallback_used=False,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=None,
        error_class=None,
        error_message=None,
    )

    # With 24h window: only the recent API call counts (local excluded, old excluded)
    count = count_api_classification_calls(conn, now_ts=now_ts, window_seconds=86400)
    assert count == 1, (
        f"Expected 1 API classification call in window; got {count}"
    )

    # Without now_ts (all-time): both API calls count (local still excluded)
    count_all = count_api_classification_calls(conn)
    assert count_all == 2, (
        f"Expected 2 API classification calls all-time (openai + deepseek); got {count_all}"
    )


@pytest.mark.unit
def test_failed_groups_cap_limits_list_not_total(tmp_path):
    """When limit=1, the list is capped to 1 row but failed_total reflects the
    real count, and count_failed_delivery_groups is unbounded."""
    from tradingagents.dashboard.panels.operations import (
        count_failed_delivery_groups,
        fetch_failed_delivery_groups,
    )

    conn = connect(str(tmp_path / "iic.db"))
    for i in range(1, 4):
        _insert_brief(conn, f"bcap{i}")
        store.insert_delivery(
            conn,
            brief_id=f"bcap{i}",
            channel="telegram",
            status="failed",
            sent_ts=None,
            channel_ref=None,
            skip_reason=None,
            delivery_group_id=f"gcap{i}",
            attempt_rank=1,
            failure_reason="timeout",
        )

    capped = fetch_failed_delivery_groups(conn, limit=1)
    total = count_failed_delivery_groups(conn)
    assert len(capped) == 1
    assert total == 3
