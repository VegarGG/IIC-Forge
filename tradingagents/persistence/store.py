"""Insert/query helpers over the SQLite store.

Each function takes an open ``sqlite3.Connection`` and commits before returning.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional


# --------------------------------------------------------------------
# runs
# --------------------------------------------------------------------

def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ticker: str,
    persona_id: Optional[str],
    started_ts: str,
    artifact_dir: str,
    trigger_id: Optional[str] = None,
    queue_job_id: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, ticker, persona_id, started_ts, status, "
        "trigger_id, artifact_dir, queue_job_id) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
        (run_id, ticker, persona_id, started_ts, trigger_id, artifact_dir,
         queue_job_id),
    )
    conn.commit()


def finalize_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ended_ts: str,
    status: str,
    decision: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    conn.execute(
        "UPDATE runs SET ended_ts = ?, status = ?, decision = ?, confidence = ? "
        "WHERE run_id = ?",
        (ended_ts, status, decision, confidence, run_id),
    )
    conn.commit()


# --------------------------------------------------------------------
# costs
# --------------------------------------------------------------------

def record_cost(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    provider: str,
    model: str,
    in_tokens: int,
    out_tokens: int,
    usd_estimate: Optional[float] = None,
    cache_hit_tokens: Optional[int] = None,
    cache_miss_tokens: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT INTO costs (run_id, provider, model, in_tokens, out_tokens, "
        "usd_estimate, cache_hit_tokens, cache_miss_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, provider, model, in_tokens, out_tokens, usd_estimate,
         cache_hit_tokens, cache_miss_tokens),
    )
    conn.commit()


# --------------------------------------------------------------------
# briefs
# --------------------------------------------------------------------

def insert_brief(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    mode: str,
    scope: str,
    generated_ts: str,
    content_path: str,
    run_ids: Iterable[str],
    parent_brief_id: Optional[str] = None,
    trigger_event_id: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO briefs (brief_id, mode, scope, generated_ts, content_path, "
        "run_ids, parent_brief_id, trigger_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (brief_id, mode, scope, generated_ts, content_path,
         json.dumps(list(run_ids)), parent_brief_id, trigger_event_id),
    )
    conn.commit()


# --------------------------------------------------------------------
# brief_actions
# --------------------------------------------------------------------

def insert_brief_action(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    action_type: str,
    action_params: dict,
    expires_at: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO brief_actions (brief_id, action_type, action_params, "
        "state, expires_at) VALUES (?, ?, ?, 'pending', ?)",
        (brief_id, action_type, json.dumps(action_params), expires_at),
    )
    conn.commit()
    return cur.lastrowid


# --------------------------------------------------------------------
# F3 helpers — events / event_ticker / watchlist / tickers / fingerprints
# --------------------------------------------------------------------

import json as _json
from datetime import datetime as _dt, timezone as _tz


def _now_iso() -> str:
    return _dt.now(_tz.utc).isoformat()


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    source: str,
    ingested_ts: str,
    salience: Optional[float],
    raw_path: Optional[str],
    status: str,
    deduped_of: Optional[str],
    salience_source: Optional[str] = None,
) -> None:
    """Insert one events row.

    ``salience_source`` (Task 15): 'llm' | 'cache' | 'deferred' | None.
    'deferred' marks an un-scored event (salience must be NULL) written when
    the salience LLM call failed — identifiable/retryable, never promotable.
    """
    conn.execute(
        "INSERT INTO events (event_id, source, ingested_ts, salience, "
        "raw_path, deduped_of, status, salience_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, source, ingested_ts, salience, raw_path, deduped_of,
         status, salience_source),
    )
    conn.commit()


def insert_event_ticker(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    ticker: str,
    confidence: Optional[float],
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_ticker (event_id, ticker, confidence) "
        "VALUES (?, ?, ?)",
        (event_id, ticker, confidence),
    )
    conn.commit()


def upsert_watchlist(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    ttl_until: Optional[str],
    tags: Iterable[str],
) -> None:
    """Insert or update a watchlist row.

    - On insert, sets ``added_ts = now()`` and ``last_briefed = now()``.
    - On update, preserves ``added_ts``; refreshes ``last_briefed`` and ``ttl_until``;
      merges tag set.
    """
    now = _now_iso()
    incoming_tags = sorted(set(tags))
    existing = conn.execute(
        "SELECT added_ts, tags FROM watchlist WHERE ticker = ?", (ticker,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO watchlist (ticker, added_ts, last_briefed, ttl_until, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, now, now, ttl_until, _json.dumps(incoming_tags)),
        )
    else:
        prior_tags = _json.loads(existing["tags"]) if existing["tags"] else []
        merged = sorted(set(prior_tags) | set(incoming_tags))
        conn.execute(
            "UPDATE watchlist SET last_briefed = ?, ttl_until = ?, tags = ? "
            "WHERE ticker = ?",
            (now, ttl_until, _json.dumps(merged), ticker),
        )
    conn.commit()


def get_active_watchlist(conn: sqlite3.Connection) -> list[str]:
    """Tickers that are either user-curated (ttl_until IS NULL) or not yet expired."""
    # datetime() normalizes ISO `T` + `+00:00` to SQLite's `YYYY-MM-DD HH:MM:SS`
    # form so same-day comparisons work (raw string compare silently fails when
    # one side has `T` and the other has a space).
    rows = conn.execute(
        "SELECT ticker FROM watchlist "
        "WHERE ttl_until IS NULL OR datetime(ttl_until) > datetime('now')"
    )
    return [r["ticker"] for r in rows]


def upsert_ticker(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    exchange: str,
    name: str,
    aliases: Iterable[str],
    active: bool,
) -> None:
    conn.execute(
        "INSERT INTO tickers (ticker, exchange, name, aliases, active, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "exchange = excluded.exchange, "
        "name = excluded.name, "
        "aliases = excluded.aliases, "
        "active = excluded.active, "
        "updated_ts = excluded.updated_ts",
        (ticker, exchange, name, _json.dumps(list(aliases)),
         1 if active else 0, _now_iso()),
    )
    conn.commit()


def get_tickers_set(conn: sqlite3.Connection) -> set[str]:
    """All currently-active tickers — used by ticker validator."""
    rows = conn.execute("SELECT ticker FROM tickers WHERE active = 1")
    return {r["ticker"] for r in rows}


def insert_event_fingerprint(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    kind: str,
    event_id: str,
    source: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_fingerprints "
        "(fingerprint, kind, event_id, source, created_ts) VALUES (?, ?, ?, ?, ?)",
        (fingerprint, kind, event_id, source, _now_iso()),
    )
    conn.commit()


def insert_event_embedding(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    vec_id: int,
) -> None:
    conn.execute(
        "INSERT INTO event_embeddings (event_id, vec_id, created_ts) "
        "VALUES (?, ?, ?)",
        (event_id, vec_id, _now_iso()),
    )
    conn.commit()


def insert_alert_evaluation(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    tickers: list[str],
    decision: str,
    score: float,
    payload: dict,
    created_ts: str,
    model_id: Optional[str] = None,
    parse_ok: Optional[bool] = None,
    latency_ms: Optional[int] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO alert_evaluations "
        "(event_id, tickers, decision, score, payload, created_ts, "
        "model_id, parse_ok, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            json.dumps(tickers),
            decision,
            score,
            json.dumps(payload),
            created_ts,
            model_id,
            # SQLite stores booleans as integers; None stays NULL
            (1 if parse_ok else 0) if parse_ok is not None else None,
            latency_ms,
        ),
    )
    conn.commit()
    return cur.lastrowid


def fetch_alert_eval_telemetry(
    conn: sqlite3.Connection,
    *,
    model_id: Optional[str] = None,
) -> list[dict]:
    """Return alert_evaluations rows with telemetry columns for funnel analysis.

    Columns returned: evaluation_id, event_id, decision, score, model_id,
    parse_ok, latency_ms, created_ts.

    Optionally filter by model_id.  Rows are ordered by evaluation_id
    (insertion order) — stable for per-model parse-failure rate and latency
    distribution queries (Task 14/16 consumers).
    """
    where = "WHERE model_id = ? " if model_id is not None else ""
    params: tuple = (model_id,) if model_id is not None else ()
    rows = conn.execute(
        "SELECT evaluation_id, event_id, decision, score, "
        "model_id, parse_ok, latency_ms, created_ts "
        f"FROM alert_evaluations {where}"
        "ORDER BY evaluation_id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------
# F4 helpers — events lookup / suppression / briefs lookup
# --------------------------------------------------------------------

def get_event(
    conn: sqlite3.Connection, *, event_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()


def upsert_suppression(
    conn: sqlite3.Connection,
    *,
    key: str,
    until_ts: str,
    reason: Optional[str],
    created_by: str,
) -> None:
    conn.execute(
        "INSERT INTO suppression (key, until_ts, reason, created_by) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "until_ts = excluded.until_ts, "
        "reason = excluded.reason, "
        "created_by = excluded.created_by",
        (key, until_ts, reason, created_by),
    )
    conn.commit()


def get_brief(
    conn: sqlite3.Connection, *, brief_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)
    ).fetchone()


# --------------------------------------------------------------------
# F5 deliveries + brief_actions helpers
# --------------------------------------------------------------------

def insert_delivery(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    channel: str,
    status: str,
    sent_ts: Optional[str],
    channel_ref: Optional[str],
    skip_reason: Optional[str],
    delivery_group_id: Optional[str] = None,
    attempt_rank: Optional[int] = None,
    fallback_of: Optional[int] = None,
    is_fallback: bool = False,
    failure_reason: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO deliveries (brief_id, channel, status, sent_ts, channel_ref, "
        "skip_reason, delivery_group_id, attempt_rank, fallback_of, is_fallback, "
        "failure_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            brief_id, channel, status, sent_ts, channel_ref, skip_reason,
            delivery_group_id, attempt_rank, fallback_of, 1 if is_fallback else 0,
            (failure_reason[:1000] if failure_reason else None),
        ),
    )
    conn.commit()
    return cur.lastrowid


def resolve_brief_id_by_channel_ref(
    conn: sqlite3.Connection, *, channel: str, channel_ref: str,
) -> Optional[str]:
    row = conn.execute(
        "SELECT brief_id FROM deliveries WHERE channel = ? AND channel_ref = ? "
        "ORDER BY delivery_id DESC LIMIT 1",
        (channel, channel_ref),
    ).fetchone()
    return row[0] if row else None


def count_brief_actions(conn: sqlite3.Connection, *, brief_id: str) -> int:
    """Number of brief_actions rows for a brief. Used as an idempotency guard
    so event_alert delivery creates at most one pending action per brief."""
    row = conn.execute(
        "SELECT COUNT(*) FROM brief_actions WHERE brief_id = ?", (brief_id,)
    ).fetchone()
    return row[0]


def get_pending_action_by_brief(
    conn: sqlite3.Connection, *, brief_id: str, action_type: Optional[str] = None,
) -> Optional[dict]:
    """Most recent pending brief_action for a brief (optionally by type).

    Returns None when no pending action exists — callers fall back to inserting.
    """
    if action_type is None:
        row = conn.execute(
            "SELECT * FROM brief_actions "
            "WHERE brief_id = ? AND state = 'pending' "
            "ORDER BY action_id DESC LIMIT 1",
            (brief_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM brief_actions "
            "WHERE brief_id = ? AND action_type = ? AND state = 'pending' "
            "ORDER BY action_id DESC LIMIT 1",
            (brief_id, action_type),
        ).fetchone()
    return dict(row) if row else None


def fetch_actions(conn: sqlite3.Connection, *, state: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM brief_actions WHERE state = ? ORDER BY action_id",
        (state,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_pending_run_full_study(conn: sqlite3.Connection) -> list[dict]:
    """All pending run_full_study actions (one per awaiting ticker), oldest
    first. Used by the `forge alert` CLI and the exit-gate evaluator."""
    rows = conn.execute(
        "SELECT a.*, b.trigger_event_id, b.scope "
        "FROM brief_actions a JOIN briefs b ON b.brief_id = a.brief_id "
        "WHERE a.action_type = 'run_full_study' AND a.state = 'pending' "
        "ORDER BY a.action_id",
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_accepted_undispatched(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM brief_actions "
        "WHERE state = 'accepted' "
        "  AND result_backtest_id IS NULL "
        "  AND result_brief_id IS NULL "
        "  AND result_job_id IS NULL "
        "  AND error IS NULL "
        "ORDER BY action_id"
    ).fetchall()
    return [dict(r) for r in rows]


def update_action_state(
    conn: sqlite3.Connection, *, action_id: int, state: str, responded_at: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE brief_actions SET state = ?, responded_at = ? WHERE action_id = ?",
        (state, responded_at, action_id),
    )
    conn.commit()


def mark_action_done(
    conn: sqlite3.Connection,
    *,
    action_id: int,
    result_backtest_id: Optional[int] = None,
    result_brief_id: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE brief_actions SET result_backtest_id = ?, result_brief_id = ? "
        "WHERE action_id = ?",
        (result_backtest_id, result_brief_id, action_id),
    )
    conn.commit()


def mark_action_dispatched(
    conn: sqlite3.Connection,
    *,
    action_id: int,
    result_job_id: int,
    dispatched_ts: str,
) -> None:
    conn.execute(
        "UPDATE brief_actions SET result_job_id = ?, dispatched_ts = ? "
        "WHERE action_id = ?",
        (result_job_id, dispatched_ts, action_id),
    )
    conn.commit()


def mark_action_error(
    conn: sqlite3.Connection,
    *,
    action_id: int,
    error: str,
) -> None:
    conn.execute(
        "UPDATE brief_actions SET error = ? WHERE action_id = ?",
        (error[:1000], action_id),
    )
    conn.commit()


def mark_full_study_action_done_for_job(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    result_brief_id: str,
) -> None:
    conn.execute(
        "UPDATE brief_actions SET result_brief_id = ? "
        "WHERE action_type = 'run_full_study' AND result_job_id = ?",
        (result_brief_id, job_id),
    )
    conn.commit()


def expire_lapsed_actions(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE brief_actions SET state = 'expired' "
        # datetime(expires_at) normalizes the ISO 'T'+offset string to SQLite's
        # space form so same-day expiries actually fire; a raw compare silently
        # never expires anything within the current year (S-8 hazard).
        "WHERE state = 'pending' AND datetime(expires_at) < datetime('now')"
    )
    conn.commit()
    return cur.rowcount


def load_brief(conn: sqlite3.Connection, brief_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)
    ).fetchone()
    return dict(row) if row else None


def update_brief_refine_metadata(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    refine_depth: int,
    refine_overrides: dict,
) -> None:
    conn.execute(
        "UPDATE briefs SET refine_depth = ?, refine_overrides = ? WHERE brief_id = ?",
        (refine_depth, json.dumps(refine_overrides), brief_id),
    )
    conn.commit()


def update_brief_analysis_pack(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    analysis_pack_id: str,
) -> None:
    conn.execute(
        "UPDATE briefs SET analysis_pack_id = ? WHERE brief_id = ?",
        (analysis_pack_id, brief_id),
    )
    conn.commit()


# --------------------------------------------------------------------
# Task 13: shadow_eval — per-call rows from the replay harness
# --------------------------------------------------------------------

def insert_shadow_eval(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    model_id: str,
    parse_ok: bool,
    created_ts: str,
    api_salience: Optional[float] = None,
    local_salience: Optional[float] = None,
    salience_delta: Optional[float] = None,
    api_verdict: Optional[str] = None,
    local_verdict: Optional[str] = None,
    latency_ms: Optional[int] = None,
) -> int:
    """Insert one shadow-replay row and return its shadow_id.

    A row may cover the triage role only (salience columns set, verdict columns
    NULL), the alert-gate role only (verdict columns set, salience columns NULL),
    or both roles simultaneously.  ``parse_ok`` records whether the LOCAL model
    response was parseable; ``latency_ms`` is the LOCAL call's wall-clock time.
    """
    cur = conn.execute(
        "INSERT INTO shadow_eval "
        "(event_id, model_id, api_salience, local_salience, salience_delta, "
        "api_verdict, local_verdict, parse_ok, latency_ms, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            model_id,
            api_salience,
            local_salience,
            salience_delta,
            api_verdict,
            local_verdict,
            1 if parse_ok else 0,
            latency_ms,
            created_ts,
        ),
    )
    conn.commit()
    return cur.lastrowid


def fetch_shadow_eval(
    conn: sqlite3.Connection,
    *,
    model_id: Optional[str] = None,
    limit: Optional[int] = None,
    newest: bool = False,
) -> list[dict]:
    """Return shadow_eval rows for the Task-14 reporter.

    Columns returned: shadow_id, event_id, model_id, api_salience,
    local_salience, salience_delta, api_verdict, local_verdict, parse_ok,
    latency_ms, created_ts.

    Optionally filter by ``model_id``.  Optionally cap results with ``limit``.

    When ``newest=False`` (default): rows ordered by shadow_id ASC, LIMIT
    takes the *oldest* N rows — stable insertion-order slice.

    When ``newest=True``: LIMIT takes the *newest* N rows (ORDER BY
    shadow_id DESC internally) then re-sorts them ascending by shadow_id
    before returning, so callers always receive a time-ordered slice.  Use
    this for ``--report-only --limit N`` to ensure the reporter sees the most
    recent run rather than a stale prefix.
    """
    where = "WHERE model_id = ? " if model_id is not None else ""
    params = (model_id,) if model_id is not None else ()

    if newest and limit is not None:
        # Fetch the newest N rows (DESC), then re-sort ascending for the caller.
        inner_sql = (
            "SELECT shadow_id, event_id, model_id, "
            "api_salience, local_salience, salience_delta, "
            "api_verdict, local_verdict, parse_ok, latency_ms, created_ts "
            f"FROM shadow_eval {where}ORDER BY shadow_id DESC LIMIT {int(limit)}"
        )
        sql = f"SELECT * FROM ({inner_sql}) ORDER BY shadow_id ASC"
    else:
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        sql = (
            "SELECT shadow_id, event_id, model_id, "
            "api_salience, local_salience, salience_delta, "
            "api_verdict, local_verdict, parse_ok, latency_ms, created_ts "
            f"FROM shadow_eval {where}ORDER BY shadow_id ASC {limit_clause}"
        )

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------
# Task 15 helpers — ops_counters (availability failure counters / budgets)
# --------------------------------------------------------------------
# Persistent named counters consumed by the D5 availability layer
# (tradingagents/llm_clients/availability.py) and read by the L3 soak gate
# ("failure counter = 0" must be queryable across daemon restarts) and the
# Task 17 endpoint-down self-alert.  Counter names in use are documented on
# the ops_counters table in schema.sql.

def bump_ops_counter(
    conn: sqlite3.Connection, *, name: str, delta: int = 1
) -> int:
    """Atomically add ``delta`` to counter ``name`` (creating it at ``delta``)
    and return the new value."""
    conn.execute(
        "INSERT INTO ops_counters (name, value, updated_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "value = value + excluded.value, "
        "updated_ts = excluded.updated_ts",
        (name, delta, _now_iso()),
    )
    conn.commit()
    return get_ops_counter(conn, name=name)


def get_ops_counter(conn: sqlite3.Connection, *, name: str) -> int:
    """Current value of counter ``name``; 0 when the counter does not exist."""
    row = conn.execute(
        "SELECT value FROM ops_counters WHERE name = ?", (name,)
    ).fetchone()
    return int(row["value"]) if row is not None else 0


# --------------------------------------------------------------------
# Service platform reconstruction control-plane helpers
# --------------------------------------------------------------------

def _bool_to_int(value: Optional[bool]) -> Optional[int]:
    return None if value is None else (1 if value else 0)


def insert_llm_call(
    conn: sqlite3.Connection,
    *,
    created_ts: str,
    role: str,
    service_name: str,
    provider: str,
    model_id: str,
    base_url: Optional[str],
    request_kind: str,
    linked_type: str,
    linked_id: Optional[str],
    status: str,
    latency_ms: Optional[int],
    parse_ok: Optional[bool],
    fallback_mode: Optional[str],
    fallback_used: bool,
    in_tokens: Optional[int],
    out_tokens: Optional[int],
    cache_hit_tokens: Optional[int],
    cache_miss_tokens: Optional[int],
    usd_estimate: Optional[float],
    error_class: Optional[str],
    error_message: Optional[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO llm_calls (created_ts, role, service_name, provider, "
        "model_id, base_url, request_kind, linked_type, linked_id, status, "
        "latency_ms, parse_ok, fallback_mode, fallback_used, in_tokens, "
        "out_tokens, cache_hit_tokens, cache_miss_tokens, usd_estimate, "
        "error_class, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_ts, role, service_name, provider, model_id, base_url,
            request_kind, linked_type, linked_id, status, latency_ms,
            _bool_to_int(parse_ok), fallback_mode, 1 if fallback_used else 0,
            in_tokens, out_tokens, cache_hit_tokens, cache_miss_tokens,
            usd_estimate, error_class, (error_message[:1000] if error_message else None),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def fetch_llm_calls(conn: sqlite3.Connection, *, role: Optional[str] = None) -> list[dict]:
    if role is None:
        rows = conn.execute("SELECT * FROM llm_calls ORDER BY call_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM llm_calls WHERE role = ? ORDER BY call_id",
            (role,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_source_health_success(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    last_poll_ts: str,
    last_success_ts: str,
    last_event_ts: Optional[str],
    cursor: Optional[str],
    cursor_updated_ts: Optional[str],
    events_emitted_last_poll: int,
    diagnostics: Optional[dict] = None,
) -> None:
    conn.execute(
        "INSERT INTO source_health (source, service_name, last_poll_ts, "
        "last_success_ts, last_event_ts, cursor, cursor_updated_ts, "
        "events_emitted_total, events_emitted_last_poll, consecutive_failures, "
        "last_error, last_error_ts, diagnostics) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "service_name = excluded.service_name, "
        "last_poll_ts = excluded.last_poll_ts, "
        "last_success_ts = excluded.last_success_ts, "
        "last_event_ts = COALESCE(excluded.last_event_ts, source_health.last_event_ts), "
        "cursor = COALESCE(excluded.cursor, source_health.cursor), "
        "cursor_updated_ts = COALESCE(excluded.cursor_updated_ts, source_health.cursor_updated_ts), "
        "events_emitted_total = source_health.events_emitted_total + excluded.events_emitted_last_poll, "
        "events_emitted_last_poll = excluded.events_emitted_last_poll, "
        "consecutive_failures = 0, "
        "last_error = NULL, "
        "last_error_ts = NULL, "
        "diagnostics = excluded.diagnostics",
        (
            source, service_name, last_poll_ts, last_success_ts, last_event_ts,
            cursor, cursor_updated_ts, events_emitted_last_poll,
            events_emitted_last_poll, json.dumps(diagnostics or {}),
        ),
    )
    conn.commit()


def upsert_source_health_failure(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    last_poll_ts: str,
    error: str,
    diagnostics: Optional[dict] = None,
) -> None:
    conn.execute(
        "INSERT INTO source_health (source, service_name, last_poll_ts, "
        "events_emitted_last_poll, consecutive_failures, last_error, "
        "last_error_ts, diagnostics) "
        "VALUES (?, ?, ?, 0, 1, ?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "service_name = excluded.service_name, "
        "last_poll_ts = excluded.last_poll_ts, "
        "events_emitted_last_poll = 0, "
        "consecutive_failures = source_health.consecutive_failures + 1, "
        "last_error = excluded.last_error, "
        "last_error_ts = excluded.last_error_ts, "
        "diagnostics = excluded.diagnostics",
        (
            source, service_name, last_poll_ts, error[:1000], last_poll_ts,
            json.dumps(diagnostics or {}),
        ),
    )
    conn.commit()


def fetch_source_health(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    return {r["source"]: dict(r) for r in rows}


def find_active_deferred_salience_retry(
    conn: sqlite3.Connection,
    *,
    payload_hash: str,
) -> Optional[int]:
    """Return the retry_id of an existing pending or running row for this
    payload_hash, or None if no such row exists.

    Uses ``idx_deferred_salience_retry_payload`` to avoid a full scan.
    """
    row = conn.execute(
        "SELECT retry_id FROM deferred_salience_retry "
        "WHERE payload_hash = ? AND state IN ('pending', 'running') "
        "ORDER BY retry_id LIMIT 1",
        (payload_hash,),
    ).fetchone()
    return int(row["retry_id"]) if row is not None else None


def insert_deferred_salience_retry(
    conn: sqlite3.Connection,
    *,
    event_id: Optional[str],
    source: str,
    raw_path: Optional[str],
    payload_hash: str,
    payload_json: str,
    reason: str,
    next_attempt_ts: str,
) -> int:
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO deferred_salience_retry (event_id, source, raw_path, "
        "payload_hash, payload_json, reason, next_attempt_ts, state, "
        "last_error, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            event_id, source, raw_path, payload_hash, payload_json,
            reason[:500], next_attempt_ts, reason[:1000], now, now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def claim_due_deferred_salience_retries(
    conn: sqlite3.Connection,
    *,
    now_ts: str,
    limit: int,
) -> list[dict]:
    # Atomic claim: single UPDATE ... RETURNING prevents double-claiming when
    # multiple claimers race (requires SQLite >= 3.35, available since 3.51.2).
    # Returned rows carry POST-increment attempt_count (first claim = 1).
    rows = conn.execute(
        "UPDATE deferred_salience_retry "
        "SET state='running', attempt_count=attempt_count+1, "
        "last_attempt_ts=?, updated_ts=? "
        "WHERE retry_id IN ("
        "    SELECT retry_id FROM deferred_salience_retry "
        "    WHERE state='pending' AND datetime(next_attempt_ts) <= datetime(?) "
        "    ORDER BY next_attempt_ts, retry_id LIMIT ?"
        ") RETURNING *",
        (now_ts, now_ts, now_ts, int(limit)),
    ).fetchall()
    conn.commit()
    return [dict(r) for r in rows]


def reclaim_stale_running_retries(
    conn: sqlite3.Connection,
    *,
    older_than_ts: str,
) -> int:
    """Re-pend running rows whose last update is older than the cutoff (claimer died)."""
    now = _now_iso()
    cur = conn.execute(
        "UPDATE deferred_salience_retry SET state='pending', updated_ts=? "
        "WHERE state='running' AND datetime(updated_ts) <= datetime(?)",
        (now, older_than_ts),
    )
    conn.commit()
    return cur.rowcount


def reschedule_deferred_salience_retry(
    conn: sqlite3.Connection,
    *,
    retry_id: int,
    reason: str,
    next_attempt_ts: str,
) -> None:
    now = _now_iso()
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'pending', reason = ?, "
        "last_error = ?, next_attempt_ts = ?, updated_ts = ? WHERE retry_id = ?",
        (reason[:500], reason[:1000], next_attempt_ts, now, retry_id),
    )
    conn.commit()


def mark_deferred_salience_retry_done(conn: sqlite3.Connection, *, retry_id: int) -> None:
    now = _now_iso()
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'done', updated_ts = ? WHERE retry_id = ?",
        (now, retry_id),
    )
    conn.commit()


def mark_deferred_salience_retry_dead(
    conn: sqlite3.Connection,
    *,
    retry_id: int,
    reason: str,
) -> None:
    now = _now_iso()
    conn.execute(
        "UPDATE deferred_salience_retry SET state = 'dead', last_error = ?, updated_ts = ? "
        "WHERE retry_id = ?",
        (reason[:1000], now, retry_id),
    )
    conn.commit()


def fetch_deferred_salience_retries(
    conn: sqlite3.Connection,
    *,
    state: Optional[str] = None,
) -> list[dict]:
    if state is None:
        rows = conn.execute(
            "SELECT * FROM deferred_salience_retry ORDER BY retry_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deferred_salience_retry WHERE state = ? ORDER BY retry_id",
            (state,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_delivery_groups(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT * FROM deliveries WHERE delivery_group_id IS NOT NULL "
        "ORDER BY delivery_group_id, attempt_rank, delivery_id"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["delivery_group_id"], []).append(dict(row))
    return out
