#!/usr/bin/env python
"""Combined F4/F5 approval-delivery exit-gate evaluator."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, date, timedelta, timezone
from typing import Any, Optional

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.run_recorder import compute_cache_hit_ratio
from tradingagents.persistence.db import connect


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), int(k) + 1
    if c >= len(s):
        return s[-1]
    return s[f] + (s[c] - s[f]) * (k - f)


def _seconds(a: str, b: str) -> float:
    aa = datetime.fromisoformat(a.replace("Z", "+00:00"))
    bb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return (bb - aa).total_seconds()


def _row_count(row: sqlite3.Row | None) -> int:
    return int(row[0] or 0) if row is not None else 0


def _cost_cache_summary(conn: sqlite3.Connection, since: datetime, until: datetime) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          COUNT(DISTINCT r.run_id) AS runs,
          COALESCE(SUM(c.in_tokens), 0) AS in_tokens,
          COALESCE(SUM(c.out_tokens), 0) AS out_tokens,
          COALESCE(SUM(c.cache_hit_tokens), 0) AS cache_hit_tokens,
          COALESCE(SUM(c.cache_miss_tokens), 0) AS cache_miss_tokens,
          COALESCE(SUM(c.usd_estimate), 0.0) AS usd_estimate
        FROM runs r
        LEFT JOIN costs c ON c.run_id = r.run_id
        WHERE r.started_ts BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone()
    hit = int(row["cache_hit_tokens"] or 0)
    miss = int(row["cache_miss_tokens"] or 0)
    return {
        "runs": int(row["runs"] or 0),
        "in_tokens": int(row["in_tokens"] or 0),
        "out_tokens": int(row["out_tokens"] or 0),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_hit_ratio": compute_cache_hit_ratio(hit, miss),
        "usd_estimate": float(row["usd_estimate"] or 0.0),
    }


def evaluate(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    window_hours: int,
) -> dict[str, Any]:
    until = since + timedelta(hours=window_hours)
    checks: dict[str, dict[str, Any]] = {}

    light_rows = list(conn.execute(
        """
        SELECT b.brief_id, b.generated_ts, b.trigger_event_id, e.ingested_ts
        FROM briefs b JOIN events e ON e.event_id = b.trigger_event_id
        WHERE b.mode = 'event_alert_light'
          AND b.generated_ts BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ))
    latencies = [_seconds(r["ingested_ts"], r["generated_ts"]) for r in light_rows]
    p95 = _percentile(latencies, 0.95) if latencies else 0.0
    checks["light_alert_latency"] = {
        "pass": bool(light_rows) and p95 <= 300,
        "detail": f"{len(light_rows)} light alerts, p95={p95:.1f}s",
    }

    delivered_light = _row_count(conn.execute(
        """
        SELECT COUNT(DISTINCT b.brief_id)
        FROM briefs b JOIN deliveries d ON d.brief_id = b.brief_id
        WHERE b.mode = 'event_alert_light'
          AND d.status IN ('sent', 'skipped')
          AND b.generated_ts BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    checks["light_delivery_audit"] = {
        "pass": delivered_light >= len(light_rows),
        "detail": (
            f"{delivered_light}/{len(light_rows)} light alerts have "
            "sent/skipped delivery rows"
        ),
    }

    event_count = len({r["trigger_event_id"] for r in light_rows})
    passed_evals = _row_count(conn.execute(
        """
        SELECT COUNT(DISTINCT ae.event_id)
        FROM alert_evaluations ae
        JOIN briefs b ON b.trigger_event_id = ae.event_id
        WHERE b.mode = 'event_alert_light'
          AND b.generated_ts BETWEEN ? AND ?
          AND ae.decision = 'pass'
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    rejected_evals = _row_count(conn.execute(
        """
        SELECT COUNT(*)
        FROM alert_evaluations ae
        WHERE ae.created_ts BETWEEN ? AND ?
          AND ae.decision != 'pass'
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    checks["alert_quality_audit"] = {
        "pass": event_count >= 1 and passed_evals >= event_count,
        "detail": (
            f"{passed_evals}/{event_count} light-alert events passed strict "
            f"evaluation; rejects={rejected_evals}"
        ),
    }

    accepted = _row_count(conn.execute(
        """
        SELECT COUNT(*)
        FROM brief_actions
        WHERE action_type = 'run_full_study'
          AND state = 'accepted'
          AND responded_at BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    lineage = _row_count(conn.execute(
        """
        SELECT COUNT(DISTINCT a.action_id)
        FROM brief_actions a
        JOIN queue_jobs q ON q.job_id = a.result_job_id
        JOIN briefs fb ON fb.brief_id = a.result_brief_id
        WHERE a.action_type = 'run_full_study'
          AND a.state = 'accepted'
          AND q.state = 'done'
          AND fb.mode = 'event_alert'
          AND fb.parent_brief_id = a.brief_id
          AND a.responded_at BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    checks["approval_lineage"] = {
        "pass": accepted >= 1 and lineage == accepted,
        "detail": (
            f"{lineage}/{accepted} accepted actions completed a done job "
            "and linked full brief"
        ),
    }

    full_delivered = _row_count(conn.execute(
        """
        SELECT COUNT(DISTINCT b.brief_id)
        FROM briefs b JOIN deliveries d ON d.brief_id = b.brief_id
        WHERE b.mode = 'event_alert'
          AND b.parent_brief_id IS NOT NULL
          AND d.status IN ('sent', 'skipped')
          AND b.generated_ts BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    checks["full_brief_delivery"] = {
        "pass": full_delivered >= lineage,
        "detail": (
            f"{full_delivered}/{lineage} full briefs have sent/skipped "
            "delivery rows"
        ),
    }

    errors = _row_count(conn.execute(
        """
        SELECT COUNT(*)
        FROM queue_jobs
        WHERE state = 'error'
          AND enqueued_ts BETWEEN ? AND ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone())
    checks["worker_errors"] = {
        "pass": errors == 0,
        "detail": f"{errors} queue job errors",
    }

    summaries = {
        "cost_cache": _cost_cache_summary(conn, since, until),
        "operator_signoff": {
            "false_positive_sample_required": True,
            "note": "Operator must sample rejected and passed alert evaluations.",
        },
    }
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "checks": checks,
        "summaries": summaries,
        "pass": all(c["pass"] for c in checks.values()),
    }


def render_md(report: dict[str, Any]) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [
        f"# F4/F5 Combined Exit-Gate Report - {today}",
        "",
        f"**Window:** `{report['since']}` to `{report['until']}`",
        "",
        f"**Overall:** {'PASS' if report['pass'] else 'FAIL'}",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for name, check in report["checks"].items():
        result = "PASS" if check["pass"] else "FAIL"
        lines.append(f"| {name} | {result} | {check['detail']} |")

    cost = report["summaries"]["cost_cache"]
    ratio = cost["cache_hit_ratio"]
    ratio_text = "n/a" if ratio is None else f"{ratio:.1%}"
    lines += [
        "",
        "## Cost And Cache Summary",
        "",
        f"- runs: {cost['runs']}",
        f"- input/output tokens: {cost['in_tokens']} / {cost['out_tokens']}",
        f"- cache hit/miss tokens: {cost['cache_hit_tokens']} / {cost['cache_miss_tokens']}",
        f"- cache hit ratio: {ratio_text}",
        f"- estimated cost: ${cost['usd_estimate']:.4f}",
        "",
        "## Operator Sign-Off",
        "",
        "- [ ] Review a false-positive/false-negative sample from alert evaluations.",
        "- [ ] Confirm accepted approval lineage maps light alert -> job -> full brief.",
        "- [ ] Confirm full briefs were delivered or explicitly skipped.",
    ]
    return "\n".join(lines)


def soak_report(
    conn: sqlite3.Connection,
    *,
    local_model_id: Optional[str] = None,
    day: Optional[str] = None,
) -> dict[str, Any]:
    """Aggregate cutover-soak counters for the post-cutover monitoring period.

    Returns a dict with:
      local_gate_calls       — alert_evaluations rows for the local model
                               (filtered by local_model_id when given; all rows
                               when None — use for a total-volume check when
                               local is the only gate model post-cutover)
      gate_parse_failures    — local gate evaluations where parse_ok = 0
      triage_events_scored   — events with salience_source = 'llm'
                               NOTE: this is the total triage-scored event count;
                               it cannot distinguish local vs API triage calls
                               because the events table carries no model_id column
                               (triage telemetry gains per-provider attribution only
                               on the gate side via alert_evaluations.model_id).
                               Interpret as: "events the triage LLM handled" —
                               which equals total LLM-scored events post-cutover
                               when the local model is the sole triage provider.
      triage_events_deferred — events with salience_source = 'deferred'
                               (salience LLM failed; these are un-scored and
                               never promoted — should be 0 post-cutover)
      triage_llm_failures    — ops_counter 'triage_llm_failures' (monotonic)
      promoter_llm_failures  — ops_counter 'promoter_llm_failures' (monotonic)
      fallback_calls_today   — {triage: int, promoter: int} budget counters for
                               ``day`` (default: today UTC)
      costs                  — fetch_provider_split dict:
                               {local_calls, api_calls, free_calls,
                                unknown_calls, api_spend}
                               Post-cutover target: api_spend -> 0 for
                               gate/triage workloads.

    Post-cutover healthy state:
      triage_llm_failures = 0, promoter_llm_failures = 0,
      gate_parse_failures = 0, fallback_calls_today = {triage:0, promoter:0},
      costs.api_spend -> 0.
    """
    from tradingagents.dashboard.panels.costs import fetch_provider_split
    from tradingagents.llm_clients.availability import (
        TRIAGE_FAILURE_COUNTER,
        PROMOTER_FAILURE_COUNTER,
        TRIAGE_FALLBACK_BUDGET,
        PROMOTER_FALLBACK_BUDGET,
    )
    from tradingagents.persistence import store

    today = day or date.today().isoformat()

    # -- gate: local model call volume & parse failures ----------------------
    if local_model_id is not None:
        gate_rows = conn.execute(
            "SELECT parse_ok FROM alert_evaluations WHERE model_id = ?",
            (local_model_id,),
        ).fetchall()
    else:
        gate_rows = conn.execute(
            "SELECT parse_ok FROM alert_evaluations"
        ).fetchall()

    local_gate_calls = len(gate_rows)
    gate_parse_failures = sum(
        1 for r in gate_rows if not r["parse_ok"]
    )

    # -- triage: event salience_source counts --------------------------------
    triage_events_scored = _row_count(
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE salience_source = 'llm'"
        ).fetchone()
    )
    triage_events_deferred = _row_count(
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE salience_source = 'deferred'"
        ).fetchone()
    )

    # -- ops_counters: failure totals ----------------------------------------
    triage_llm_failures = store.get_ops_counter(
        conn, name=TRIAGE_FAILURE_COUNTER
    )
    promoter_llm_failures = store.get_ops_counter(
        conn, name=PROMOTER_FAILURE_COUNTER
    )

    # -- daily fallback budget counters (per day) ----------------------------
    fallback_triage = store.get_ops_counter(
        conn, name=f"{TRIAGE_FALLBACK_BUDGET}:{today}"
    )
    fallback_promoter = store.get_ops_counter(
        conn, name=f"{PROMOTER_FALLBACK_BUDGET}:{today}"
    )

    # -- cost split ----------------------------------------------------------
    costs = fetch_provider_split(conn)

    return {
        "local_gate_calls": local_gate_calls,
        "gate_parse_failures": gate_parse_failures,
        "triage_events_scored": triage_events_scored,
        "triage_events_deferred": triage_events_deferred,
        "triage_llm_failures": triage_llm_failures,
        "promoter_llm_failures": promoter_llm_failures,
        "fallback_calls_today": {
            "triage": fallback_triage,
            "promoter": fallback_promoter,
        },
        "costs": costs,
    }


def render_soak_md(report: dict[str, Any]) -> str:
    """Render soak_report as a Markdown section."""
    costs = report["costs"]
    fb = report["fallback_calls_today"]
    lines = [
        "## Soak Report — Local Model Cutover Counters",
        "",
        f"- local gate calls:          {report['local_gate_calls']}",
        f"- gate parse failures:       {report['gate_parse_failures']}",
        f"- triage events scored (llm):{report['triage_events_scored']}",
        f"- triage events deferred:    {report['triage_events_deferred']}",
        f"- triage LLM failures:       {report['triage_llm_failures']}",
        f"- promoter LLM failures:     {report['promoter_llm_failures']}",
        f"- fallback calls today:      triage={fb['triage']} promoter={fb['promoter']}",
        "",
        "### Cost Split",
        f"- local calls:   {costs['local_calls']}",
        f"- api calls:     {costs['api_calls']}",
        f"- free calls:    {costs['free_calls']}",
        f"- unknown calls: {costs['unknown_calls']}",
        f"- api spend:     ${costs['api_spend']:.6f}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None)
    parser.add_argument("--window-hours", type=int, default=12)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--soak-report",
        action="store_true",
        help="Print the soak/cutover counter report instead of the gate report.",
    )
    parser.add_argument(
        "--local-model-id",
        default=None,
        help="Local model id for soak_report (filters alert_evaluations).",
    )
    parser.add_argument(
        "--day",
        default=None,
        help="Day (YYYY-MM-DD) for fallback budget counters (default: today UTC).",
    )
    args = parser.parse_args()

    conn = connect(DEFAULT_CONFIG["iic_db_path"])

    if args.soak_report:
        report = soak_report(conn, local_model_id=args.local_model_id, day=args.day)
        if args.json:
            sys.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            sys.stdout.write(render_soak_md(report))
            sys.stdout.write("\n")
        return 0

    if args.since is None:
        parser.error("--since is required unless --soak-report is given")

    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    report = evaluate(conn, since=since, window_hours=args.window_hours)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, default=str))
    else:
        sys.stdout.write(render_md(report))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
