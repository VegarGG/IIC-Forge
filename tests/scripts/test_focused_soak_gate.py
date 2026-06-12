"""Tests for scripts/focused_soak_gate.py — Task 11 focused soak gate.

Covers:
- Healthy seed passes all checks (soak mode).
- Stale source fails sources_fresh.
- Amendment B: orphaned deferred event fails deferred_retry_bounded.
- Amendment A: API classification call in llm_calls fails spend check
  even when costs.api_spend == 0.
- Amendment C: preflight mode skips sources_fresh and llm_calls_present.
"""

from __future__ import annotations

import pytest

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_healthy(conn, now_ts: str = "2026-06-12T10:00:00+00:00") -> None:
    """Seed a minimal healthy state: one fresh source + one local llm_call."""
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts=now_ts,
        last_success_ts=now_ts,
        last_event_ts=now_ts,
        cursor="c1",
        cursor_updated_ts=now_ts,
        events_emitted_last_poll=1,
        diagnostics={},
    )
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
        linked_id="ev1",
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


def _default_checkers():
    return dict(
        old_service_checker=lambda: [],
        redis_checker=lambda: {"ok": True, "appendonly": "yes"},
    )


# ---------------------------------------------------------------------------
# Plan baseline tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_focused_gate_passes_with_healthy_seed(tmp_path):
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    assert report["pass"] is True
    assert report["checks"]["sources_fresh"]["pass"] is True
    assert report["checks"]["llm_calls_present"]["pass"] is True


@pytest.mark.unit
def test_focused_gate_fails_stale_source(tmp_path):
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    # Source polled at 09:00; gate runs at 10:05 (65 min gap > 30 min stale)
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts="2026-06-12T09:00:00+00:00",
        last_success_ts="2026-06-12T09:00:00+00:00",
        last_event_ts="2026-06-12T09:00:00+00:00",
        cursor="c1",
        cursor_updated_ts="2026-06-12T09:00:00+00:00",
        events_emitted_last_poll=1,
        diagnostics={},
    )

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    assert report["pass"] is False
    assert report["checks"]["sources_fresh"]["pass"] is False


# ---------------------------------------------------------------------------
# Amendment C: preflight mode skips sources_fresh and llm_calls_present
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_preflight_skips_sources_fresh_and_llm_calls_present(tmp_path):
    """In preflight mode, sources_fresh and llm_calls_present are marked pass
    even when the DB is empty (no source rows, no llm_calls rows)."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    # Intentionally no source health rows, no llm_calls rows.

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        mode="preflight",
        **_default_checkers(),
    )
    assert report["checks"]["sources_fresh"]["pass"] is True
    assert "preflight" in report["checks"]["sources_fresh"]["detail"].lower()
    assert report["checks"]["llm_calls_present"]["pass"] is True
    assert "preflight" in report["checks"]["llm_calls_present"]["detail"].lower()


@pytest.mark.unit
def test_soak_mode_does_not_skip_sources_fresh(tmp_path):
    """In soak mode, sources_fresh is NOT skipped; missing source fails it."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    # No source health rows → gdelt is "missing" → stale.

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        mode="soak",
        **_default_checkers(),
    )
    assert report["checks"]["sources_fresh"]["pass"] is False


@pytest.mark.unit
def test_soak_mode_does_not_skip_llm_calls_present(tmp_path):
    """In soak mode, llm_calls_present is NOT skipped; empty DB fails it."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    # No llm_calls rows, but seed a fresh source so only llm check fails.
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

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        mode="soak",
        **_default_checkers(),
    )
    assert report["checks"]["llm_calls_present"]["pass"] is False


# ---------------------------------------------------------------------------
# Amendment A: API classification call via llm_calls fails spend check
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_api_classification_call_fails_spend_check_even_when_costs_zero(tmp_path):
    """API-provider classification call in llm_calls fails spend check
    even when costs.api_spend == 0 (no run-scoped spend).

    Amendment A: the check reads llm_calls rows with role IN
    ('triage_salience', 'alert_gate') AND provider NOT IN ('local').
    """
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    # Seed a fresh source so sources_fresh passes.
    now_ts = "2026-06-12T10:00:00+00:00"
    store.upsert_source_health_success(
        conn,
        source="gdelt",
        service_name="adapter-gdelt",
        last_poll_ts=now_ts,
        last_success_ts=now_ts,
        last_event_ts=now_ts,
        cursor="c1",
        cursor_updated_ts=now_ts,
        events_emitted_last_poll=1,
        diagnostics={},
    )
    # Insert an API-provider classification call (provider='openai').
    # costs table has no rows → api_spend == 0 (no run-scoped costs).
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
        usd_estimate=None,  # NULL usd — not reflected in costs table
        error_class=None,
        error_message=None,
    )

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,  # must gate-fail
        **_default_checkers(),
    )
    check = report["checks"]["no_unexpected_api_classification_spend"]
    assert check["pass"] is False, (
        "Expected spend check to fail when API classification call exists in llm_calls "
        f"even with costs.api_spend==0; detail: {check['detail']}"
    )
    assert "api_classification_calls=1" in check["detail"]


@pytest.mark.unit
def test_local_classification_call_passes_spend_check(tmp_path):
    """Local-provider classification calls must NOT trigger the spend check."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    check = report["checks"]["no_unexpected_api_classification_spend"]
    assert check["pass"] is True
    assert "api_classification_calls=0" in check["detail"]


@pytest.mark.unit
def test_allow_api_classification_spend_bypasses_check(tmp_path):
    """When allow_api_classification_spend=True, the check always passes
    regardless of llm_calls content."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    now_ts = "2026-06-12T10:00:00+00:00"
    _seed_healthy(conn, now_ts)
    # Also add an API classification call.
    store.insert_llm_call(
        conn,
        created_ts=now_ts,
        role="alert_gate",
        service_name="triage",
        provider="deepseek",
        model_id="deepseek-chat",
        base_url=None,
        request_kind="structured",
        linked_type="event",
        linked_id="ev_api2",
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

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=True,
        **_default_checkers(),
    )
    assert report["checks"]["no_unexpected_api_classification_spend"]["pass"] is True


# ---------------------------------------------------------------------------
# Amendment B: orphaned deferred event fails deferred_retry_bounded
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_orphaned_deferred_event_fails_deferred_check(tmp_path):
    """An orphaned deferred event (salience_source='deferred', salience IS NULL,
    no pending/running/done retry row) must fail deferred_retry_bounded even
    when pending count == 0.

    Amendment B: detail string must include orphaned count and
    oldest_pending_age_seconds.
    """
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    now_ts = "2026-06-12T10:00:00+00:00"
    _seed_healthy(conn, now_ts)

    # Insert an event with salience_source='deferred' and salience=NULL
    # without any corresponding deferred_salience_retry row → orphaned.
    conn.execute(
        "INSERT INTO events (event_id, source, ingested_ts, salience, "
        "salience_source, raw_path, status, deduped_of) "
        "VALUES (?, 'rss', ?, NULL, 'deferred', NULL, 'pending', NULL)",
        ("orphan_ev", now_ts),
    )
    conn.commit()

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    check = report["checks"]["deferred_retry_bounded"]
    assert check["pass"] is False, (
        f"Expected deferred check to fail with orphaned event; detail: {check['detail']}"
    )
    assert "orphaned=1" in check["detail"]
    assert "oldest_pending_age_seconds" in check["detail"]


@pytest.mark.unit
def test_no_deferred_orphans_passes_deferred_check(tmp_path):
    """No deferred orphans and pending == 0 → deferred check passes."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    assert report["checks"]["deferred_retry_bounded"]["pass"] is True
    assert "orphaned=0" in report["checks"]["deferred_retry_bounded"]["detail"]


# ---------------------------------------------------------------------------
# Stable check names
# ---------------------------------------------------------------------------

EXPECTED_CHECK_NAMES = {
    "old_services_stopped",
    "redis_owned_and_configured",
    "sources_fresh",
    "deferred_retry_bounded",
    "llm_calls_present",
    "llm_failures_bounded",
    "no_unexpected_api_classification_spend",
    "delivery_groups_bounded",
}


@pytest.mark.unit
def test_evaluate_returns_all_expected_check_names(tmp_path):
    """evaluate() must return exactly the 8 stable check names."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    assert set(report["checks"].keys()) == EXPECTED_CHECK_NAMES


# ---------------------------------------------------------------------------
# Old services / Redis probe checks
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_old_service_still_active_fails_check(tmp_path):
    """old_service_checker returning a non-empty list → old_services_stopped fails."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        old_service_checker=lambda: ["iic-triage.service"],
        redis_checker=lambda: {"ok": True, "appendonly": "yes"},
    )
    assert report["checks"]["old_services_stopped"]["pass"] is False
    assert report["pass"] is False


@pytest.mark.unit
def test_redis_appendonly_not_yes_fails_check(tmp_path):
    """redis appendonly != 'yes' → redis_owned_and_configured fails."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        old_service_checker=lambda: [],
        redis_checker=lambda: {"ok": True, "appendonly": "no"},
    )
    assert report["checks"]["redis_owned_and_configured"]["pass"] is False


# ---------------------------------------------------------------------------
# delivery_groups_bounded uses failed_total (unbounded count, not capped list)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_delivery_groups_bounded_uses_failed_total(tmp_path):
    """Gate uses failed_total (unbounded) not len(failed_list[:50])."""
    from scripts.focused_soak_gate import evaluate

    conn = connect(str(tmp_path / "iic.db"))
    _seed_healthy(conn, "2026-06-12T10:00:00+00:00")

    # Insert a brief (required by FK) then a failed delivery group.
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert_light",
        scope='["AAPL"]',
        generated_ts="2026-06-12T10:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    conn.execute(
        "INSERT INTO deliveries (brief_id, channel, status, delivery_group_id) "
        "VALUES ('b1', 'telegram', 'failed', 'grp-1')"
    )
    conn.commit()

    report = evaluate(
        conn,
        now_ts="2026-06-12T10:05:00+00:00",
        enabled_sources=["gdelt"],
        source_stale_after_seconds=1800,
        deferred_pending_max=0,
        failed_delivery_group_max=0,
        allow_api_classification_spend=False,
        **_default_checkers(),
    )
    check = report["checks"]["delivery_groups_bounded"]
    assert check["pass"] is False
    assert "failed_groups=1" in check["detail"]
