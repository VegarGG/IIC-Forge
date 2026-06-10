"""Shadow-eval replay harness + acceptance report (IIC-FORGE_05 Task 14).

Replays the last N stored events/candidates through BOTH the API quick model
and a local candidate endpoint, writes per-call ``shadow_eval`` rows, and
prints the acceptance report.  Run once per candidate model
(``--model qwen3.6-27b-instruct-q4_k_m`` then
``--model deepseek-v4-flash-gguf-q4_k_m``) and let the numbers pick the model.

Reused seams (no duplicated prompt/parse logic):
  * raw-text read pattern — mirrors the promoter exactly
    (tradingagents/orchestrator/promoter.py): ``store.get_event`` ->
    ``raw_path`` -> ``Path.read_text(encoding="utf-8", errors="replace")``.
  * salience — ``build_salience_prompt`` (sensing.prompts) +
    ``salience._parse`` (think-strip -> fence-strip -> SalienceSchema
    validation; importable without a Redis SERVER, only the redis package) +
    ``maybe_bind_salience_schema`` for capability-gated json_schema binding.
  * alert gate — ``evaluate_alert_candidate`` (orchestrator.alert_evaluator)
    used directly for both sides; it already does capability-gated binding,
    think-stripping, and telemetry (parse_ok / latency_ms).
  * persistence — ``store.insert_shadow_eval`` / ``store.fetch_shadow_eval``.

Method/measurement decisions (documented per the task):
  * Verdicts are stored as 'pass'/'reject' strings derived at the 0.9
    evaluator-score operating point (``min_score=--confidence-threshold``,
    default 0.9) for BOTH the API and the local side, so verdict agreement IS
    the threshold-crossing agreement at the 0.9 confidence operating point.
  * Cohen's kappa is computed by hand: kappa = (po - pe) / (1 - pe) with pe
    from the raters' marginals.  Degenerate pe == 1 (both raters constant on
    the same class) -> kappa = 1.0 if po == 1 else 0.0 (the po < 1 branch is
    unreachable from real marginals but kept defensive).
  * Percentiles use linear interpolation between closest ranks
    (numpy's default 'linear' method): idx = p/100 * (n-1) over sorted values.
  * latency_ms in shadow_eval rows is the LOCAL call's wall-clock time.  The
    "local p95 <= API p95" gate line needs an API reference: ``--api-p95-ms``
    wins when given; otherwise the p95 of ``alert_evaluations`` telemetry rows
    whose model_id != candidate (NULL model_id counts as API-side legacy rows)
    is used; when neither exists the line prints "n/a".

Run-separation and append-only semantics:
  * shadow_eval rows are append-only across runs — there is NO run_id column.
    Re-runs of the same model mix into the same table; subsequent reports will
    span multiple replay sessions unless ``--limit`` bounds the query to the
    newest run.
  * ``--report-only --limit N`` always fetches the NEWEST N rows (using
    ``fetch_shadow_eval(..., newest=True)``).  After a 500-event replay that
    writes ~1 000 rows, ``--limit 1000`` will report on exactly that run.
  * KeyboardInterrupt during replay leaves all already-committed rows intact
    (each ``insert_shadow_eval`` commits immediately).  Re-running simply
    appends new rows; use ``--limit`` to scope the reporter to the new run.

CLI:
  python scripts/shadow_eval.py --model qwen3.6-27b-instruct-q4_k_m
  python scripts/shadow_eval.py --model cand --report-only        # no network
  python scripts/shadow_eval.py --model cand --persist-set        # Phase D seed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Repo-root import guard so `python scripts/shadow_eval.py` works standalone
# from any cwd even without an installed tradingagents package.  Idempotent;
# no other import-time side effects (tests import this module directly).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tradingagents.persistence import store
from tradingagents.persistence.db import connect


# ---------------------------------------------------------------------------
# Pure metric helpers (unit-tested; no I/O)
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], p: float) -> float:
    """p-th percentile via linear interpolation between closest ranks
    (numpy 'linear'): idx = p/100 * (n-1) over the sorted values."""
    if not values:
        raise ValueError("percentile() of empty sequence")
    xs = sorted(float(v) for v in values)
    idx = (p / 100.0) * (len(xs) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(xs) - 1)
    frac = idx - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def cohen_kappa(pairs: Sequence[Tuple[str, str]]) -> float:
    """Cohen's kappa over (rater_a, rater_b) label pairs, by hand (no sklearn).

    kappa = (po - pe) / (1 - pe), pe from the two raters' marginals.
    Degenerate pe == 1 -> 1.0 if po == 1 else 0.0 (documented; the po < 1
    branch cannot arise from real marginals but is kept defensive).
    """
    if not pairs:
        raise ValueError("cohen_kappa() of empty sequence")
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    labels = {a for a, _ in pairs} | {b for _, b in pairs}
    pe = 0.0
    for lbl in labels:
        pa = sum(1 for a, _ in pairs if a == lbl) / n
        pb = sum(1 for _, b in pairs if b == lbl) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1.0 - pe)


def compute_report(
    rows: Sequence[Dict[str, Any]],
    *,
    salience_threshold: float = 0.85,
    confidence_threshold: float = 0.9,
    api_p95_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """Acceptance report over shadow_eval rows (fetch_shadow_eval shape).

    Metric families:
      * salience MAE + threshold-crossing agreement at ``salience_threshold``
        (rows where both saliences are present);
      * verdict raw agreement + Cohen's kappa (rows where both verdicts are
        present; verdicts were derived at the ``confidence_threshold``
        operating point when the rows were written);
      * p50/p95 of latency_ms (local path; linear interpolation);
      * parse-failure rate (fraction parse_ok == 0);
      * L2 gate verdicts: salience agreement >= 0.95, verdict agreement
        >= 0.90, parse failures == 0, local p95 <= API p95 (None -> "n/a"
        when a side is missing).
    """
    n_rows = len(rows)

    # --- salience family -------------------------------------------------
    sal_pairs = [
        (float(r["api_salience"]), float(r["local_salience"]))
        for r in rows
        if r.get("api_salience") is not None
        and r.get("local_salience") is not None
    ]
    mae = (
        sum(abs(a - l) for a, l in sal_pairs) / len(sal_pairs)
        if sal_pairs else None
    )
    sal_agree = (
        sum(
            1 for a, l in sal_pairs
            if (a >= salience_threshold) == (l >= salience_threshold)
        ) / len(sal_pairs)
        if sal_pairs else None
    )

    # --- verdict family ---------------------------------------------------
    ver_pairs = [
        (str(r["api_verdict"]), str(r["local_verdict"]))
        for r in rows
        if r.get("api_verdict") is not None
        and r.get("local_verdict") is not None
    ]
    ver_agree = (
        sum(1 for a, l in ver_pairs if a == l) / len(ver_pairs)
        if ver_pairs else None
    )
    kappa = cohen_kappa(ver_pairs) if ver_pairs else None

    # --- latency family (local path) --------------------------------------
    lats = [float(r["latency_ms"]) for r in rows
            if r.get("latency_ms") is not None]
    p50 = percentile(lats, 50) if lats else None
    p95 = percentile(lats, 95) if lats else None

    # --- parse-failure rate -----------------------------------------------
    failures = sum(1 for r in rows if not r.get("parse_ok"))
    failure_rate = (failures / n_rows) if n_rows else None
    # API-side salience failures: rows where api_salience is NULL (API call
    # failed or was absent) — run-time visibility only, no schema change.
    api_failures = sum(
        1 for r in rows
        if r.get("api_salience") is None and r.get("local_salience") is not None
    )

    # --- L2 gate verdicts ---------------------------------------------------
    gate = {
        "salience_agreement_target": 0.95,
        "verdict_agreement_target": 0.90,
        "salience_agreement_pass":
            (sal_agree >= 0.95) if sal_agree is not None else None,
        "verdict_agreement_pass":
            (ver_agree >= 0.90) if ver_agree is not None else None,
        "parse_failures_pass": (failures == 0) if n_rows else None,
        "latency_pass":
            (p95 <= api_p95_ms)
            if (p95 is not None and api_p95_ms is not None) else None,
    }

    return {
        "n_rows": n_rows,
        "salience": {
            "n_pairs": len(sal_pairs),
            "mae": mae,
            "threshold": salience_threshold,
            "threshold_agreement": sal_agree,
        },
        "verdict": {
            "n_pairs": len(ver_pairs),
            "agreement": ver_agree,
            "kappa": kappa,
            "operating_point": confidence_threshold,
        },
        "latency": {
            "n": len(lats),
            "p50_ms": p50,
            "p95_ms": p95,
            "api_p95_ms": api_p95_ms,
        },
        "parse": {
            "n_rows": n_rows,
            "failures": failures,
            "failure_rate": failure_rate,
            "api_failures": api_failures,
        },
        "gate": gate,
    }


def _fmt(v: Optional[float], spec: str = ".3f") -> str:
    return "n/a" if v is None else format(v, spec)


def _fmt_ms(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f}ms"


def _gate_line(label: str, ok: Optional[bool], detail: str) -> str:
    verdict = "n/a" if ok is None else ("PASS" if ok else "FAIL")
    return f"  {label}: {verdict} ({detail})"


def format_report(report: Dict[str, Any], *, model_id: str = "") -> str:
    """Human-readable acceptance report including the L2 gate verdict lines."""
    sal, ver = report["salience"], report["verdict"]
    lat, par, gate = report["latency"], report["parse"], report["gate"]
    lines = [
        f"shadow_eval report — model: {model_id or 'n/a'}",
        f"rows: {report['n_rows']}",
        (
            f"salience: n_pairs={sal['n_pairs']} mae={_fmt(sal['mae'], '.4f')} "
            f"agreement@{sal['threshold']:g}="
            f"{_fmt(sal['threshold_agreement'])}"
        ),
        (
            f"verdict (operating point: evaluator min_score="
            f"{ver['operating_point']:g}): n_pairs={ver['n_pairs']} "
            f"agreement={_fmt(ver['agreement'])} kappa={_fmt(ver['kappa'])}"
        ),
        (
            f"latency (local): n={lat['n']} "
            f"p50={_fmt_ms(lat['p50_ms'])} "
            f"p95={_fmt_ms(lat['p95_ms'])}"
        ),
        (
            f"parse failures: {par['failures']}/{par['n_rows']} "
            f"(rate={_fmt(par['failure_rate'])}) "
            f"api_failures={par.get('api_failures', 0)}"
        ),
        "L2 gate:",
        _gate_line(
            "salience agreement >= 0.95",
            gate["salience_agreement_pass"],
            _fmt(sal["threshold_agreement"]),
        ),
        _gate_line(
            "verdict agreement >= 0.90",
            gate["verdict_agreement_pass"],
            _fmt(ver["agreement"]),
        ),
        _gate_line(
            "parse failures == 0",
            gate["parse_failures_pass"],
            str(par["failures"]),
        ),
        _gate_line(
            "local p95 <= API p95",
            gate["latency_pass"],
            (
                f"local={_fmt_ms(lat['p95_ms'])} "
                f"api={_fmt_ms(lat['api_p95_ms'])}"
                if lat["api_p95_ms"] is not None
                else "no API latency reference"
            ),
        ),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB-side helpers (promoter's raw-path read pattern; API p95 reference)
# ---------------------------------------------------------------------------

def read_event_raw_text(conn, event_id: str) -> Optional[str]:
    """Mirror the promoter's raw-path read pattern exactly:
    store.get_event -> raw_path -> Path.read_text(utf-8, errors='replace')."""
    ev = store.get_event(conn, event_id=event_id)
    if ev is None or not ev["raw_path"]:
        return None
    p = Path(ev["raw_path"])
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def load_replay_events(conn, *, limit: int) -> List[Dict[str, Any]]:
    """Last N triaged events that have stored raw text, newest first.

    Each item: {event_id, source, ingested_ts, salience, raw_path, raw_text,
    tickers: [(ticker, confidence), ...]}.  Events whose raw file has gone
    missing are skipped (same tolerance as the promoter's existence check).
    """
    rows = conn.execute(
        "SELECT event_id, source, ingested_ts, salience, raw_path "
        "FROM events "
        "WHERE status = 'triaged' AND raw_path IS NOT NULL AND raw_path != '' "
        "ORDER BY ingested_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        raw_text = read_event_raw_text(conn, r["event_id"])
        if raw_text is None:
            continue
        tickers = conn.execute(
            "SELECT ticker, confidence FROM event_ticker WHERE event_id = ?",
            (r["event_id"],),
        ).fetchall()
        out.append({
            "event_id": r["event_id"],
            "source": r["source"],
            "ingested_ts": r["ingested_ts"],
            "salience": r["salience"],
            "raw_path": r["raw_path"],
            "raw_text": raw_text,
            "tickers": [(t["ticker"], t["confidence"]) for t in tickers],
        })
    return out


def resolve_api_p95(
    conn, *, candidate_model_id: str, api_p95_ms: Optional[float]
) -> Optional[float]:
    """API-latency reference for the p95 gate line.

    ``--api-p95-ms`` wins.  Otherwise: p95 of alert_evaluations telemetry rows
    whose model_id differs from the candidate (NULL model_id = legacy API-side
    rows).  None when no reference exists ("n/a" in the report).
    """
    if api_p95_ms is not None:
        return api_p95_ms
    telemetry = store.fetch_alert_eval_telemetry(conn)
    lats = [
        float(t["latency_ms"]) for t in telemetry
        if t["latency_ms"] is not None and t["model_id"] != candidate_model_id
    ]
    return percentile(lats, 95) if lats else None


# ---------------------------------------------------------------------------
# Replay (network path) — both roles per event
# ---------------------------------------------------------------------------

def build_local_config(
    cfg: Dict[str, Any], *, model: str, base_url: Optional[str]
) -> Dict[str, Any]:
    """Copy of cfg whose triage_salience/alert_gate roles route to the local
    candidate.  base_url=None lets openai_client resolve LOCAL_LLM_BASE_URL
    (or the localhost default) per-request."""
    local_cfg = dict(cfg)
    roles = {k: dict(v) for k, v in cfg.get("llm_roles", {}).items()}
    for role in ("triage_salience", "alert_gate"):
        roles[role] = {
            "provider": "local",
            "model": model,
            "base_url": base_url,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            "fallback": "none",
        }
    local_cfg["llm_roles"] = roles
    return local_cfg


def score_salience_once(
    llm: Any, *, env: Any, watchlist: Sequence[str]
) -> Tuple[Optional[float], bool, int]:
    """One salience call: build_salience_prompt -> invoke -> salience._parse.

    Returns (salience | None, parse_ok, latency_ms).  ``_parse`` is the
    salience module's own parser (strip_think_blocks -> fence-strip ->
    SalienceSchema validation) so no parsing logic is duplicated here.
    """
    from tradingagents.sensing.prompts import build_salience_prompt
    from tradingagents.sensing.salience import _parse as parse_salience

    prompt = build_salience_prompt(env=env, watchlist=list(watchlist),
                                   macro_context="")
    t0 = time.monotonic()
    try:
        resp = llm.invoke(prompt)
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = parse_salience(getattr(resp, "content", str(resp)))
        return result.salience, True, latency_ms
    except Exception:
        return None, False, int((time.monotonic() - t0) * 1000)


def replay(
    conn,
    *,
    cfg: Dict[str, Any],
    model: str,
    limit: int,
    local_base_url: Optional[str],
    confidence_threshold: float,
) -> List[Dict[str, Any]]:
    """Replay the last N events through API + local for both roles.

    Writes one shadow_eval row PER CALL: a triage row per event (salience
    columns; parse_ok/latency_ms = the LOCAL salience call) and a gate row for
    promotable candidates (verdict columns; parse_ok/latency_ms = the LOCAL
    gate call).  Returns the written rows (fetch_shadow_eval shape, plus
    ``raw_text`` for --persist-set).
    """
    from tradingagents.llm_clients.factory import create_role_llm
    from tradingagents.orchestrator.alert_evaluator import (
        evaluate_alert_candidate,
    )
    from tradingagents.sensing.envelope import Envelope
    from tradingagents.sensing.salience import maybe_bind_salience_schema

    # API side from the global config; local side via role override.
    api_triage = create_role_llm("triage_salience", cfg)
    api_gate = create_role_llm("alert_gate", cfg)
    local_cfg = build_local_config(cfg, model=model, base_url=local_base_url)
    local_triage = create_role_llm("triage_salience", local_cfg)
    local_gate = create_role_llm("alert_gate", local_cfg)

    api_triage_llm = maybe_bind_salience_schema(
        api_triage.get_llm(), api_triage.model)
    local_triage_llm = maybe_bind_salience_schema(
        local_triage.get_llm(), local_triage.model)
    api_gate_llm = api_gate.get_llm()
    local_gate_llm = local_gate.get_llm()

    watchlist = store.get_active_watchlist(conn)
    salience_promote = cfg.get("alert_salience_threshold", 0.85)
    ticker_conf = cfg.get("alert_ticker_confidence_threshold", 0.8)

    events = load_replay_events(conn, limit=limit)
    n_events = len(events)
    rows: List[Dict[str, Any]] = []
    for i, ev in enumerate(events, start=1):
        created_ts = datetime.now(timezone.utc).isoformat()
        env = Envelope(
            source=ev["source"], ingested_ts=ev["ingested_ts"],
            external_id="", text=ev["raw_text"], source_tags={},
            raw_path=ev["raw_path"],
        )

        # --- triage role: one row per event ------------------------------
        api_sal, _api_ok, _ = score_salience_once(
            api_triage_llm, env=env, watchlist=watchlist)
        loc_sal, loc_ok, loc_lat = score_salience_once(
            local_triage_llm, env=env, watchlist=watchlist)
        delta = (
            loc_sal - api_sal
            if (api_sal is not None and loc_sal is not None) else None
        )
        shadow_id = store.insert_shadow_eval(
            conn,
            event_id=ev["event_id"], model_id=model,
            api_salience=api_sal, local_salience=loc_sal,
            salience_delta=delta,
            parse_ok=loc_ok, latency_ms=loc_lat, created_ts=created_ts,
        )
        rows.append({
            "shadow_id": shadow_id, "event_id": ev["event_id"],
            "model_id": model,
            "api_salience": api_sal, "local_salience": loc_sal,
            "salience_delta": delta,
            "api_verdict": None, "local_verdict": None,
            "parse_ok": 1 if loc_ok else 0, "latency_ms": loc_lat,
            "created_ts": created_ts, "raw_text": ev["raw_text"],
        })

        # --- gate role: promotable candidates only ------------------------
        tickers = [
            t for t, c in ev["tickers"]
            if c is not None and c >= ticker_conf
        ]
        promotable = (
            bool(tickers)
            and ev["salience"] is not None
            and ev["salience"] >= salience_promote
        )

        api_verdict: Optional[str] = None
        local_verdict: Optional[str] = None
        gate_parse_ok: bool = True
        gate_lat: int = 0

        if promotable:
            # Wrap each gate call independently so a network/parse failure on
            # one side does not abort the entire replay run.
            # KeyboardInterrupt is NOT caught (Exception does not cover it).
            api_eval = None
            try:
                api_eval = evaluate_alert_candidate(
                    llm=api_gate_llm, event_text=ev["raw_text"],
                    tickers=tickers, min_score=confidence_threshold,
                    model_id=api_gate.model,
                )
                api_verdict = "pass" if api_eval.passed else "reject"
            except Exception as exc:
                print(
                    f"[shadow_eval] gate api-side error for {ev['event_id']}: "
                    f"{exc}",
                    file=sys.stderr, flush=True,
                )
                gate_parse_ok = False

            loc_eval = None
            try:
                loc_eval = evaluate_alert_candidate(
                    llm=local_gate_llm, event_text=ev["raw_text"],
                    tickers=tickers, min_score=confidence_threshold,
                    model_id=local_gate.model,
                )
                local_verdict = "pass" if loc_eval.passed else "reject"
                gate_parse_ok = gate_parse_ok and bool(loc_eval.parse_ok)
                gate_lat = loc_eval.latency_ms or 0
            except Exception as exc:
                print(
                    f"[shadow_eval] gate local-side error for {ev['event_id']}: "
                    f"{exc}",
                    file=sys.stderr, flush=True,
                )
                gate_parse_ok = False

            shadow_id = store.insert_shadow_eval(
                conn,
                event_id=ev["event_id"], model_id=model,
                api_verdict=api_verdict, local_verdict=local_verdict,
                parse_ok=gate_parse_ok,
                latency_ms=gate_lat, created_ts=created_ts,
            )
            rows.append({
                "shadow_id": shadow_id, "event_id": ev["event_id"],
                "model_id": model,
                "api_salience": None, "local_salience": None,
                "salience_delta": None,
                "api_verdict": api_verdict, "local_verdict": local_verdict,
                "parse_ok": 1 if gate_parse_ok else 0,
                "latency_ms": gate_lat,
                "created_ts": created_ts, "raw_text": ev["raw_text"],
            })

        # --- per-event progress line to stderr (Issue 2) -----------------
        _sal_api = _fmt(api_sal) if api_sal is not None else "err"
        _sal_loc = _fmt(loc_sal) if loc_sal is not None else "err"
        _gate_api = api_verdict if api_verdict is not None else (
            "skip" if not promotable else "err"
        )
        _gate_loc = local_verdict if local_verdict is not None else (
            "skip" if not promotable else "err"
        )
        print(
            f"[{i}/{n_events}] {ev['event_id']} "
            f"sal(api={_sal_api}, loc={_sal_loc}) "
            f"gate(api={_gate_api}, loc={_gate_loc})",
            file=sys.stderr, flush=True,
        )

    return rows


# ---------------------------------------------------------------------------
# --persist-set: seed of the FORGE_04 Phase D labeled corpus
# ---------------------------------------------------------------------------

def default_persist_path(model_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", model_id)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"shadow_eval_set_{safe}_{ts}.json"


def build_persist_set(
    conn, *, model_id: str, rows: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Replay-set JSON: event ids + raw text + both-model outputs.

    Hand-label ~50 disagreements from this file during review (FORGE_04
    Phase D corpus seed).  ``raw_text`` comes with replay rows directly; for
    --report-only rows it is re-read via the promoter pattern and is null
    when the event/raw file no longer exists.
    """
    out_rows = []
    for r in rows:
        raw_text = r.get("raw_text")
        if raw_text is None:
            raw_text = read_event_raw_text(conn, r["event_id"])
        out_rows.append({
            "event_id": r["event_id"],
            "raw_text": raw_text,
            "api_salience": r.get("api_salience"),
            "local_salience": r.get("local_salience"),
            "salience_delta": r.get("salience_delta"),
            "api_verdict": r.get("api_verdict"),
            "local_verdict": r.get("local_verdict"),
            "parse_ok": r.get("parse_ok"),
            "latency_ms": r.get("latency_ms"),
        })
    return {
        "model_id": model_id,
        "created_ts": datetime.now(timezone.utc).isoformat(),
        "n_rows": len(out_rows),
        "rows": out_rows,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shadow_eval",
        description="Replay stored events through the API quick model and a "
                    "local candidate; write shadow_eval rows; print the "
                    "acceptance report.",
    )
    parser.add_argument(
        "--model", required=True,
        help="local candidate model id (e.g. qwen3.6-27b-instruct-q4_k_m)")
    parser.add_argument(
        "--limit", type=int, default=500,
        help="number of events to replay (or rows to report on with "
             "--report-only); default 500")
    parser.add_argument(
        "--db", default=None,
        help="sqlite db path; default: config iic_db_path")
    parser.add_argument(
        "--report-only", action="store_true",
        help="skip replay; report over existing shadow_eval rows for --model")
    parser.add_argument(
        "--persist-set", action="store_true",
        help="write the replay set (event ids + raw text + both-model "
             "outputs) as JSON — the FORGE_04 Phase D corpus seed")
    parser.add_argument(
        "--persist-set-path", default=None,
        help="override the --persist-set output path "
             "(default shadow_eval_set_<model>_<ts>.json)")
    parser.add_argument(
        "--local-base-url", default=os.environ.get("LOCAL_LLM_BASE_URL"),
        help="local endpoint base URL; default: LOCAL_LLM_BASE_URL env "
             "(further per-request resolution applies when unset)")
    parser.add_argument(
        "--api-p95-ms", type=float, default=None,
        help="explicit API p95 latency reference for the gate line; "
             "default: derived from alert_evaluations telemetry when present")
    parser.add_argument(
        "--salience-threshold", type=float, default=0.85,
        help="salience threshold-crossing operating point (default 0.85)")
    parser.add_argument(
        "--confidence-threshold", type=float, default=0.9,
        help="evaluator-score operating point used to derive verdicts and "
             "documented in the report (default 0.9)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    db_path = args.db
    cfg: Dict[str, Any] = {}
    if db_path is None or not args.report_only:
        # Lazy import: config is only needed to resolve the default db path
        # and (for replay) role routing/thresholds.
        from tradingagents.default_config import DEFAULT_CONFIG
        cfg = dict(DEFAULT_CONFIG)
        if db_path is None:
            db_path = cfg["iic_db_path"]

    conn = connect(db_path)
    try:
        if args.report_only:
            rows: List[Dict[str, Any]] = store.fetch_shadow_eval(
                conn, model_id=args.model, limit=args.limit, newest=True)
        else:
            rows = replay(
                conn,
                cfg=cfg,
                model=args.model,
                limit=args.limit,
                local_base_url=args.local_base_url,
                confidence_threshold=args.confidence_threshold,
            )

        report = compute_report(
            rows,
            salience_threshold=args.salience_threshold,
            confidence_threshold=args.confidence_threshold,
            api_p95_ms=resolve_api_p95(
                conn, candidate_model_id=args.model,
                api_p95_ms=args.api_p95_ms,
            ),
        )
        print(format_report(report, model_id=args.model))

        if args.persist_set:
            path = args.persist_set_path or default_persist_path(args.model)
            payload = build_persist_set(conn, model_id=args.model, rows=rows)
            Path(path).write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
            print(f"persist set written: {path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
