"""F4 promoter — polls events for trigger candidates and enqueues jobs.

Runs as `iic-promoter.service`. Defensive retry-internal: never raises out
of the main loop except on truly unrecoverable errors.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from tradingagents.persistence import store
from tradingagents.persistence.db import connect
from tradingagents.orchestrator.candidates import fetch_candidates, fetch_candidates_grouped
from tradingagents.orchestrator.guards import QueueBackpressure, QueueRateGuard


log = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def run_once(
    conn: sqlite3.Connection,
    *,
    salience_threshold: float,
    ticker_conf_threshold: float,
    batch_size: int,
    cooldown_min: int,
    backpressure: Optional[QueueBackpressure] = None,
    rate_guard: Optional[QueueRateGuard] = None,
    secretary=None,
    approval_gate_enabled: bool = False,
    pending_ttl_hours: int = 24,
    alert_evaluator=None,
) -> int:
    """Perform one poll cycle. With the approval gate enabled, composes one
    light alert per event (no study enqueued). Returns the count of light
    alerts (gate) or jobs (legacy) created."""
    if backpressure is not None and not backpressure.gate(conn):
        return 0
    if rate_guard is not None and not rate_guard.gate(conn):
        return 0

    if approval_gate_enabled:
        if secretary is None:
            raise ValueError("run_once: approval_gate_enabled requires a secretary")
        groups = fetch_candidates_grouped(
            conn, salience_threshold=salience_threshold,
            ticker_conf_threshold=ticker_conf_threshold, limit=batch_size,
        )
        # Intra-batch dedup: the candidate query reflects the suppression table
        # as it was when the fetch ran, but each compose writes new same-day
        # suppressions mid-loop that the already-fetched batch can't see. Without
        # an in-pass guard, several events naming the same ticker in one batch
        # each spawn a light alert before suppression takes hold (cross-cycle
        # dedup works; intra-batch did not). Track tickers alerted THIS pass and
        # strip them from later events so a ticker fires at most once per pass.
        seen_tickers: set = set()
        composed = 0
        for g in groups:
            fresh = [t for t in g["tickers"] if t not in seen_tickers]
            if not fresh:
                continue
            if alert_evaluator is not None:
                evaluation = alert_evaluator(g["event_id"], fresh)
                store.insert_alert_evaluation(
                    conn,
                    event_id=g["event_id"],
                    tickers=fresh,
                    decision="pass" if evaluation.passed else "reject",
                    score=evaluation.score,
                    payload=evaluation.payload,
                    created_ts=_now_utc().isoformat(),
                    model_id=evaluation.model_id,
                    parse_ok=evaluation.parse_ok,
                    latency_ms=evaluation.latency_ms,
                )
                if not evaluation.passed:
                    log.info(
                        "light alert rejected event_id=%s tickers=%s "
                        "disqualifiers=%s",
                        g["event_id"], fresh, evaluation.disqualifiers,
                    )
                    continue
            try:
                secretary.compose_event_alert_light(
                    event_id=g["event_id"], tickers=fresh,
                    ttl_hours=pending_ttl_hours, deliver=True,
                )
                seen_tickers.update(fresh)
                composed += 1
                log.info("light alert composed event_id=%s tickers=%s",
                         g["event_id"], fresh)
            except Exception:
                # Operator note: transport failures inside compose are
                # swallowed HERE (per-event, the pass continues) and are NOT
                # counted by the availability counter — only gate-evaluator
                # transport failures reach main's TRANSPORT_EXCEPTIONS
                # handler.  A partial outage that breaks compose but not the
                # gate is therefore visible in this log line, not in
                # promoter_llm_failures.  Deliberate scope (D5).
                log.exception("light alert failed event_id=%s; continuing",
                              g["event_id"])
        return composed

    # ----- Legacy auto-enqueue path (approval gate disabled) -----
    candidates = fetch_candidates(
        conn,
        salience_threshold=salience_threshold,
        ticker_conf_threshold=ticker_conf_threshold,
        limit=batch_size,
    )
    if not candidates:
        return 0

    enqueued = 0
    for ev in candidates:
        until_ts = (_now_utc() + timedelta(minutes=cooldown_min)).isoformat()
        try:
            with conn:    # one atomic tx per event
                conn.execute(
                    "INSERT INTO queue_jobs (job_type, payload, state, "
                    "enqueued_ts, trigger_event_id) VALUES (?, ?, 'queued', ?, ?)",
                    (
                        "event_alert",
                        json.dumps({"event_id": ev["event_id"],
                                    "ticker": ev["ticker"]}),
                        _now_utc().isoformat(),
                        ev["event_id"],
                    ),
                )
                store.upsert_suppression(
                    conn,
                    key=f"event_alert:{ev['ticker']}",
                    until_ts=until_ts,
                    reason=f"alert_cooldown event_id={ev['event_id']}",
                    created_by="promoter",
                )
            enqueued += 1
            log.info("enqueued event_alert event_id=%s ticker=%s",
                     ev["event_id"], ev["ticker"])
        except sqlite3.OperationalError:
            log.exception("db error enqueueing event_id=%s; backing off",
                          ev["event_id"])
            time.sleep(2)
    return enqueued


def main(config: Optional[dict] = None) -> None:
    """systemd entry point. Defensive: never exits except on KeyboardInterrupt
    or a failed startup probe with fallback="none" (D5 — refuse to start)."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.availability import (
        PROMOTER_FAILURE_COUNTER, PROMOTER_FALLBACK_BUDGET,
        TRANSPORT_EXCEPTIONS, AvailabilityCounter, DailyFallbackBudget,
        LocalEndpointUnavailable, resolve_role_llm_global,
        resolve_role_llm_with_fallback,
    )
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)

    conn = connect(cfg["iic_db_path"])
    backpressure = QueueBackpressure(
        enabled=cfg["trigger_backpressure_enabled"],
        max_pending=cfg["trigger_backpressure_max_pending"],
    )
    rate_guard = QueueRateGuard(
        enabled=cfg["trigger_daily_rate_enabled"],
        max_per_day=cfg["trigger_daily_rate_max_jobs"],
    )

    gate_enabled = cfg["alert_approval_gate_enabled"]
    secretary = None
    alert_evaluator = None

    # D5 availability state (Task 15). The failure counter is persisted via
    # ops_counters for the soak gate (Task 16) and self-alert seam (Task 17).
    role_cfg = cfg.get("llm_roles", {}).get("alert_gate", {}) or {}
    fallback_mode = (role_cfg.get("fallback") or "none").lower()
    fallback_threshold = int(role_cfg.get("fallback_threshold", 3))
    primary_is_local = (
        (role_cfg.get("provider") or cfg.get("llm_provider") or "").lower()
        == "local"
    )
    # Counter + budget share the promoter's main conn WITHOUT a shared lock —
    # safe because this daemon is single-threaded: record_failure /
    # record_success / try_consume all run inline on the loop thread, and the
    # conn is check_same_thread=True anyway.  (Contrast triage._main, which
    # must pass ONE shared lock for its cross-thread conn.)
    #
    # Task 17 self-alert: when the consecutive run reaches the role's
    # fallback_threshold (the documented alert-threshold source — no new
    # config key), the counter fires the operator self-alert EXACTLY ONCE
    # per outage (debounced in the counter, re-armed by record_success).
    # The callback runs outside the counter lock, inline on this loop thread.
    from tradingagents.ops import self_alert
    _alert_provider = (role_cfg.get("provider") or cfg.get("llm_provider") or "").lower()
    _alert_model = role_cfg.get("model") or ""
    _alert_endpoint = role_cfg.get("base_url") or ""
    _alert_context = (
        f"role=alert_gate provider={_alert_provider} "
        f"model={_alert_model} endpoint={_alert_endpoint}"
    )
    alerter = self_alert.build_self_alerter(cfg, context=_alert_context)
    avail_counter = AvailabilityCounter(
        name=PROMOTER_FAILURE_COUNTER, conn=conn,
        alert_threshold=fallback_threshold,
        on_threshold=alerter.endpoint_down_callback)
    fallback_budget = DailyFallbackBudget(
        name=PROMOTER_FALLBACK_BUDGET,
        max_per_day=int(role_cfg.get("fallback_daily_budget", 500)),
        conn=conn,
    )
    # Mutable holder so the loop's fallback engagement can swap the evaluator
    # LLM for subsequent cycles without rebuilding the closures.
    llm_state: dict = {"llm": None, "used_fallback": False, "model": None}

    if gate_enabled:
        from tradingagents.orchestrator.alert_evaluator import evaluate_alert_candidate
        from tradingagents.secretary.service import Secretary
        # Eager startup probe (only when the role resolves to provider='local'):
        # fallback="none" → a dead endpoint REFUSES to start (raise);
        # fallback="api"  → re-resolve to the global provider (logged,
        # budget-bounded per call below). Resolved endpoint + model identity
        # is logged inside resolve_role_llm_with_fallback.
        client, used_fallback = resolve_role_llm_with_fallback(
            "alert_gate", cfg)
        llm_state["llm"] = client.get_llm()
        llm_state["used_fallback"] = used_fallback
        llm_state["model"] = client.model
        secretary = Secretary(conn=conn, data_dir=cfg["iic_data_dir"],
                              llm=llm_state["llm"])

        def alert_evaluator(event_id, tickers):
            # Each call routed through the API fallback burns the hard daily
            # budget; when exhausted, degrade back to skipping — the raise is
            # caught by the cycle-skip handler in the loop below and counted.
            if (llm_state["used_fallback"]
                    and not fallback_budget.try_consume()):
                raise LocalEndpointUnavailable(
                    f"fallback daily budget exhausted for role alert_gate "
                    f"(max={fallback_budget.max_per_day}/day)"
                )
            ev = store.get_event(conn, event_id=event_id)
            event_text = ""
            if ev is not None and ev["raw_path"]:
                from pathlib import Path
                p = Path(ev["raw_path"])
                if p.exists():
                    event_text = p.read_text(
                        encoding="utf-8", errors="replace"
                    )
            # NOTE: transport errors (openai.APIConnectionError & friends)
            # PROPAGATE out of evaluate_alert_candidate — its except tuple is
            # narrow (json/validation only; in its telemetry, latency_ms is
            # None ⇔ invoke itself raised, i.e. endpoint vs parse failure).
            # The loop below catches TRANSPORT_EXCEPTIONS and skips the cycle.
            evaluation = evaluate_alert_candidate(
                llm=llm_state["llm"],
                event_text=event_text,
                tickers=list(tickers),
                min_score=cfg.get("alert_quality_threshold", 0.80),
            )
            # Health evidence only: a parse failure (transport OK, garbage
            # output) neither increments the failure counter nor resets the
            # consecutive run — it is not transport-failure evidence, and a
            # model emitting unparseable output is not health evidence
            # either.  (Triage, by contrast, counts parse defers per event —
            # see the units note in availability.py's module docstring.)
            if evaluation.parse_ok:
                avail_counter.record_success()
            return evaluation

    log.info("promoter starting: poll=%ss cooldown=%sm guards: bp=%s rate=%s",
             cfg["promoter_poll_interval_s"], cfg["alert_cooldown_min"],
             backpressure.enabled, rate_guard.enabled)

    while True:
        try:
            run_once(
                conn,
                salience_threshold=cfg["alert_salience_threshold"],
                ticker_conf_threshold=cfg["alert_ticker_confidence_threshold"],
                batch_size=cfg["promoter_batch_size"],
                cooldown_min=cfg["alert_cooldown_min"],
                backpressure=backpressure,
                rate_guard=rate_guard,
                secretary=secretary,
                approval_gate_enabled=gate_enabled,
                pending_ttl_hours=cfg["alert_pending_ttl_hours"],
                alert_evaluator=alert_evaluator,
            )
        except KeyboardInterrupt:
            log.info("promoter shutting down on KeyboardInterrupt")
            raise
        except TRANSPORT_EXCEPTIONS as e:
            # D5 (Task 15): the gate LLM endpoint is unavailable — degrade
            # LOUDLY: count + log + SKIP the cycle (no partial writes; the
            # evaluator raise happens before any insert for that event).
            # TRANSPORT_EXCEPTIONS is a deliberately NARROW tuple — genuine
            # bugs still fall through to the broad handler below.
            avail_counter.record_failure(reason=f"{type(e).__name__}: {e}")
            log.warning(
                "promoter cycle skipped: alert_gate LLM endpoint failure "
                "(%s: %s) model=%s consecutive=%d total=%d",
                type(e).__name__, e, llm_state.get("model"),
                avail_counter.consecutive, avail_counter.total,
            )
            if (gate_enabled and fallback_mode == "api" and primary_is_local
                    and not llm_state["used_fallback"]
                    and avail_counter.consecutive >= fallback_threshold):
                # Deliberate fallback: second role resolution to the GLOBAL
                # API provider for subsequent cycles (sticky for the process
                # lifetime; every call budget-bounded in alert_evaluator).
                fb = resolve_role_llm_global("alert_gate", cfg)
                llm_state["llm"] = fb.get_llm()
                llm_state["model"] = fb.model
                llm_state["used_fallback"] = True
                # The Secretary composes with its own llm handle — swap it
                # too, or compose_event_alert_light keeps hitting the dead
                # local endpoint while gate evals burn the API budget (the
                # event never alerts and is refetched every cycle).  The
                # startup-fallback path already constructs Secretary with the
                # fallback llm; only this runtime path needs the swap.
                if secretary is not None:
                    secretary.set_llm(llm_state["llm"])
        except Exception:
            log.exception("promoter loop failure; sleeping 5s and continuing")
            time.sleep(5)
        time.sleep(cfg["promoter_poll_interval_s"])


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
