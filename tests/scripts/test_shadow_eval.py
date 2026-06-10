"""Tests for scripts/shadow_eval.py — Task 14 replay harness + report.

Covers:
- compute_report over synthetic shadow_eval rows with hand-computed
  expectations: salience MAE, threshold-crossing agreement @0.85, evaluator
  verdict agreement + Cohen's kappa, p50/p95 latency (linear interpolation),
  parse-failure rate, and the L2 gate verdict fields.
- cohen_kappa / percentile helpers directly (incl. degenerate kappa cases).
- CLI flag surface (--limit, --model, --persist-set, --report-only, --db,
  --api-p95-ms) via main(argv) against a tmp DB seeded with
  insert_shadow_eval. No network, no subprocess (the module is imported and
  main() is called directly — see the pre-existing subprocess-import env
  failure in tests/scripts/test_compare_deepseek_prompt_cache.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts import shadow_eval
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(**overrides) -> dict:
    """A full shadow_eval row dict (fetch_shadow_eval shape) with overrides."""
    base = {
        "shadow_id": 1,
        "event_id": "evt",
        "model_id": "cand-A",
        "api_salience": None,
        "local_salience": None,
        "salience_delta": None,
        "api_verdict": None,
        "local_verdict": None,
        "parse_ok": 1,
        "latency_ms": None,
        "created_ts": _now(),
    }
    base.update(overrides)
    return base


def _four_rows() -> list[dict]:
    """Hand-computable 4-row case used across report tests.

    salience pairs:  |0.9-0.8|=0.1, |0.5-0.7|=0.2, |0.95-0.9|=0.05, |0.2-0.2|=0
      -> MAE = 0.35/4 = 0.0875
    crossing @0.85:  (T,F) disagree, (F,F) agree, (T,T) agree, (F,F) agree
      -> agreement = 3/4 = 0.75
    verdict pairs:   (pass,pass) (pass,reject) (reject,reject) (reject,pass)
      -> agreement = 2/4 = 0.5
      -> kappa: po=0.5; marginals api {pass:.5, reject:.5}, local {pass:.5,
         reject:.5}; pe=0.5 -> kappa = (0.5-0.5)/(1-0.5) = 0.0
    latency [100,200,300,400] (linear interpolation between closest ranks):
      p50 -> idx 1.5 -> 250.0 ; p95 -> idx 2.85 -> 300 + 0.85*100 = 385.0
    parse_ok: one failure -> rate = 0.25
    """
    return [
        _row(event_id="e1", api_salience=0.9, local_salience=0.8,
             api_verdict="pass", local_verdict="pass",
             parse_ok=1, latency_ms=100),
        _row(event_id="e2", api_salience=0.5, local_salience=0.7,
             api_verdict="pass", local_verdict="reject",
             parse_ok=1, latency_ms=200),
        _row(event_id="e3", api_salience=0.95, local_salience=0.9,
             api_verdict="reject", local_verdict="reject",
             parse_ok=0, latency_ms=300),
        _row(event_id="e4", api_salience=0.2, local_salience=0.2,
             api_verdict="reject", local_verdict="pass",
             parse_ok=1, latency_ms=400),
    ]


# ---------------------------------------------------------------------------
# compute_report — hand-computed expectations
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_compute_report_four_row_case():
    report = shadow_eval.compute_report(_four_rows())

    assert report["n_rows"] == 4

    sal = report["salience"]
    assert sal["n_pairs"] == 4
    assert sal["mae"] == pytest.approx(0.0875)
    assert sal["threshold"] == pytest.approx(0.85)
    assert sal["threshold_agreement"] == pytest.approx(0.75)

    ver = report["verdict"]
    assert ver["n_pairs"] == 4
    assert ver["agreement"] == pytest.approx(0.5)
    assert ver["kappa"] == pytest.approx(0.0)
    assert ver["operating_point"] == pytest.approx(0.9)

    lat = report["latency"]
    assert lat["n"] == 4
    assert lat["p50_ms"] == pytest.approx(250.0)
    assert lat["p95_ms"] == pytest.approx(385.0)
    assert lat["api_p95_ms"] is None

    par = report["parse"]
    assert par["failures"] == 1
    assert par["failure_rate"] == pytest.approx(0.25)

    gate = report["gate"]
    assert gate["salience_agreement_pass"] is False    # 0.75 < 0.95
    assert gate["verdict_agreement_pass"] is False     # 0.5 < 0.90
    assert gate["parse_failures_pass"] is False        # 1 failure
    assert gate["latency_pass"] is None                # no API p95 reference


@pytest.mark.unit
def test_compute_report_gate_passes_and_api_p95():
    rows = [
        _row(event_id="e1", api_salience=0.9, local_salience=0.92,
             api_verdict="pass", local_verdict="pass",
             parse_ok=1, latency_ms=100),
        _row(event_id="e2", api_salience=0.3, local_salience=0.2,
             api_verdict="reject", local_verdict="reject",
             parse_ok=1, latency_ms=120),
    ]
    report = shadow_eval.compute_report(rows, api_p95_ms=500.0)
    gate = report["gate"]
    assert report["salience"]["threshold_agreement"] == pytest.approx(1.0)
    assert report["verdict"]["agreement"] == pytest.approx(1.0)
    assert gate["salience_agreement_pass"] is True
    assert gate["verdict_agreement_pass"] is True
    assert gate["parse_failures_pass"] is True
    # p95 of [100, 120] = 100 + 0.95*20 = 119.0 <= 500
    assert report["latency"]["p95_ms"] == pytest.approx(119.0)
    assert report["latency"]["api_p95_ms"] == pytest.approx(500.0)
    assert gate["latency_pass"] is True

    # Same rows, API reference below local p95 -> FAIL.
    report2 = shadow_eval.compute_report(rows, api_p95_ms=100.0)
    assert report2["gate"]["latency_pass"] is False


@pytest.mark.unit
def test_compute_report_mixed_partial_rows():
    """Metrics only use rows where BOTH sides are present, per family."""
    rows = [
        # salience-only pair
        _row(event_id="s1", api_salience=0.9, local_salience=0.9, parse_ok=1),
        # verdict-only pair (degenerate kappa: all-agree-same-class -> 1.0)
        _row(event_id="v1", api_verdict="pass", local_verdict="pass",
             parse_ok=1, latency_ms=50),
        # half-missing salience: excluded from salience pairs
        _row(event_id="s2", api_salience=None, local_salience=0.5, parse_ok=1),
    ]
    report = shadow_eval.compute_report(rows)
    assert report["n_rows"] == 3
    assert report["salience"]["n_pairs"] == 1
    assert report["salience"]["mae"] == pytest.approx(0.0)
    assert report["salience"]["threshold_agreement"] == pytest.approx(1.0)
    assert report["verdict"]["n_pairs"] == 1
    assert report["verdict"]["agreement"] == pytest.approx(1.0)
    assert report["verdict"]["kappa"] == pytest.approx(1.0)   # degenerate po==1
    assert report["latency"]["n"] == 1
    assert report["parse"]["failure_rate"] == pytest.approx(0.0)


@pytest.mark.unit
def test_compute_report_empty_rows():
    report = shadow_eval.compute_report([])
    assert report["n_rows"] == 0
    assert report["salience"]["n_pairs"] == 0
    assert report["salience"]["mae"] is None
    assert report["salience"]["threshold_agreement"] is None
    assert report["verdict"]["agreement"] is None
    assert report["verdict"]["kappa"] is None
    assert report["latency"]["p50_ms"] is None
    assert report["latency"]["p95_ms"] is None
    assert report["parse"]["failure_rate"] is None
    gate = report["gate"]
    assert gate["salience_agreement_pass"] is None
    assert gate["verdict_agreement_pass"] is None
    assert gate["parse_failures_pass"] is None
    assert gate["latency_pass"] is None
    # format_report must not crash on the all-None report.
    text = shadow_eval.format_report(report)
    assert "n/a" in text


# ---------------------------------------------------------------------------
# cohen_kappa / percentile helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cohen_kappa_hand_case():
    pairs = [("pass", "pass"), ("pass", "pass"),
             ("reject", "reject"), ("pass", "reject")]
    # po = 3/4; api marginals {pass: 3/4, reject: 1/4};
    # local marginals {pass: 1/2, reject: 1/2};
    # pe = 3/4*1/2 + 1/4*1/2 = 1/2 ; kappa = (0.75-0.5)/(0.5) = 0.5
    assert shadow_eval.cohen_kappa(pairs) == pytest.approx(0.5)


@pytest.mark.unit
def test_cohen_kappa_degenerate_all_agree_same_class():
    # pe == 1 (both raters always 'pass') and po == 1 -> kappa = 1.0
    assert shadow_eval.cohen_kappa([("pass", "pass")] * 3) == pytest.approx(1.0)


@pytest.mark.unit
def test_cohen_kappa_opposite_degenerate_raters():
    # api always 'pass', local always 'reject': po=0, pe=0 -> kappa = 0.0
    assert shadow_eval.cohen_kappa([("pass", "reject")] * 4) == pytest.approx(0.0)


@pytest.mark.unit
def test_percentile_linear_interpolation():
    assert shadow_eval.percentile([100.0], 95) == pytest.approx(100.0)
    assert shadow_eval.percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    assert shadow_eval.percentile([100, 200, 300, 400], 95) == pytest.approx(385.0)
    assert shadow_eval.percentile([5.0, 1.0, 3.0], 100) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# CLI surface — main(argv) against a seeded tmp DB; no network
# ---------------------------------------------------------------------------

def _seed_db(db_path: str, model_id: str = "cand-A") -> None:
    conn = connect(db_path)
    ts = _now()
    for r in _four_rows():
        store.insert_shadow_eval(
            conn,
            event_id=r["event_id"],
            model_id=model_id,
            api_salience=r["api_salience"],
            local_salience=r["local_salience"],
            salience_delta=None,
            api_verdict=r["api_verdict"],
            local_verdict=r["local_verdict"],
            parse_ok=bool(r["parse_ok"]),
            latency_ms=r["latency_ms"],
            created_ts=ts,
        )
    conn.close()


@pytest.mark.unit
def test_main_report_only(tmp_path, capsys):
    db = str(tmp_path / "iic.db")
    _seed_db(db)
    # A row for a different candidate: must be excluded by the --model filter.
    conn = connect(db)
    store.insert_shadow_eval(
        conn, event_id="other", model_id="cand-B",
        api_salience=0.1, local_salience=0.9,
        parse_ok=False, latency_ms=9999, created_ts=_now(),
    )
    conn.close()

    rc = shadow_eval.main(["--model", "cand-A", "--db", db, "--report-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cand-A" in out
    assert "rows: 4" in out
    assert "mae=0.0875" in out
    assert "agreement@0.85=0.750" in out
    assert "agreement=0.500" in out
    assert "kappa=0.000" in out
    assert "p50=250.0ms" in out
    assert "p95=385.0ms" in out
    assert "rate=0.250" in out
    # No API latency reference seeded -> the p95 gate line says n/a.
    assert "n/a" in out


@pytest.mark.unit
def test_main_report_only_respects_limit(tmp_path, capsys):
    db = str(tmp_path / "iic.db")
    _seed_db(db)
    rc = shadow_eval.main(
        ["--model", "cand-A", "--db", db, "--report-only", "--limit", "2"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "rows: 2" in out


@pytest.mark.unit
def test_main_report_only_api_p95_flag(tmp_path, capsys):
    db = str(tmp_path / "iic.db")
    _seed_db(db)
    rc = shadow_eval.main(
        ["--model", "cand-A", "--db", db, "--report-only",
         "--api-p95-ms", "500"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # local p95 = 385.0 <= 500 -> PASS
    assert "local p95 <= API p95: PASS" in out

    rc = shadow_eval.main(
        ["--model", "cand-A", "--db", db, "--report-only",
         "--api-p95-ms", "100"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "local p95 <= API p95: FAIL" in out


@pytest.mark.unit
def test_main_report_only_api_p95_from_telemetry(tmp_path, capsys):
    """Without --api-p95-ms, the API p95 reference comes from
    alert_evaluations telemetry rows NOT belonging to the candidate."""
    db = str(tmp_path / "iic.db")
    _seed_db(db)
    conn = connect(db)
    ts = _now()
    # alert_evaluations.event_id has an FK to events — seed the parent row.
    store.insert_event(
        conn, event_id="e1", source="rss", ingested_ts=ts, salience=0.9,
        raw_path=None, status="triaged", deduped_of=None,
    )
    # API-side gate calls (different model_id): latencies 100, 200 -> p95=195.
    for lat in (100, 200):
        store.insert_alert_evaluation(
            conn, event_id="e1", tickers=["NVDA"], decision="pass",
            score=0.95, payload={}, created_ts=ts,
            model_id="api-model", parse_ok=True, latency_ms=lat,
        )
    # Candidate's own gate telemetry must be excluded from the API reference.
    store.insert_alert_evaluation(
        conn, event_id="e1", tickers=["NVDA"], decision="pass",
        score=0.95, payload={}, created_ts=ts,
        model_id="cand-A", parse_ok=True, latency_ms=1,
    )
    conn.close()

    rc = shadow_eval.main(["--model", "cand-A", "--db", db, "--report-only"])
    assert rc == 0
    out = capsys.readouterr().out
    # local p95 = 385.0 > api p95 = 195.0 -> FAIL (not n/a: reference found)
    assert "local p95 <= API p95: FAIL" in out
    assert "195.0" in out


@pytest.mark.unit
def test_main_persist_set_writes_valid_json(tmp_path, capsys):
    db = str(tmp_path / "iic.db")
    _seed_db(db)
    out_path = tmp_path / "set.json"
    rc = shadow_eval.main(
        ["--model", "cand-A", "--db", db, "--report-only",
         "--persist-set", "--persist-set-path", str(out_path)]
    )
    assert rc == 0
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["model_id"] == "cand-A"
    rows = data["rows"]
    assert len(rows) == 4
    assert {r["event_id"] for r in rows} == {"e1", "e2", "e3", "e4"}
    for r in rows:
        assert "api_salience" in r and "local_salience" in r
        assert "api_verdict" in r and "local_verdict" in r
        # raw_text is null here (no events rows / raw files seeded) but the
        # key must exist — the set seeds the FORGE_04 Phase D labeled corpus.
        assert "raw_text" in r
    out = capsys.readouterr().out
    assert str(out_path) in out


# ---------------------------------------------------------------------------
# replay flow with stubbed clients — both roles, per-call rows, no network
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Minimal .invoke double; no .bind so capability gating is bypassed."""

    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, prompt):
        return _FakeResp(self._content)


class _FakeClient:
    def __init__(self, model: str, content: str) -> None:
        self.model = model
        self._llm = _FakeLLM(content)

    def get_llm(self):
        return self._llm


_SALIENCE_API = '{"salience": 0.9, "matched_tickers": [], "mentioned_tickers": [], "reason": "x"}'
_SALIENCE_LOCAL = '{"salience": 0.6, "matched_tickers": [], "mentioned_tickers": [], "reason": "x"}'
_GATE_PASS = json.dumps({
    "decision": "pass", "score": 0.95, "materiality": "m",
    "actionability": "a", "ticker_link_evidence": "t", "novelty": "n",
    "disqualifiers": [], "reasons": [],
})
_GATE_LOW_SCORE = json.dumps({
    "decision": "pass", "score": 0.5, "materiality": "m",
    "actionability": "a", "ticker_link_evidence": "t", "novelty": "n",
    "disqualifiers": [], "reasons": [],
})


@pytest.mark.unit
def test_replay_writes_per_call_rows_for_both_roles(tmp_path, monkeypatch):
    db = str(tmp_path / "iic.db")
    conn = connect(db)
    raw = tmp_path / "ev1.json"
    raw.write_text('{"title": "NVDA beats"}', encoding="utf-8")
    store.insert_event(
        conn, event_id="ev1", source="rss", ingested_ts=_now(), salience=0.9,
        raw_path=str(raw), status="triaged", deduped_of=None,
    )
    store.insert_event_ticker(conn, event_id="ev1", ticker="NVDA",
                              confidence=0.9)

    def fake_create_role_llm(role, config):
        is_local = (
            config.get("llm_roles", {})
            .get("triage_salience", {})
            .get("provider") == "local"
        )
        if role == "triage_salience":
            return _FakeClient(
                "cand-A" if is_local else "api-quick",
                _SALIENCE_LOCAL if is_local else _SALIENCE_API,
            )
        return _FakeClient(
            "cand-A" if is_local else "api-quick",
            _GATE_LOW_SCORE if is_local else _GATE_PASS,
        )

    monkeypatch.setattr(
        "tradingagents.llm_clients.factory.create_role_llm",
        fake_create_role_llm,
    )

    cfg = {
        "llm_roles": {
            "triage_salience": {"provider": None, "model": None},
            "alert_gate": {"provider": None, "model": None},
        },
        "alert_salience_threshold": 0.85,
        "alert_ticker_confidence_threshold": 0.8,
    }
    rows = shadow_eval.replay(
        conn, cfg=cfg, model="cand-A", limit=10,
        local_base_url=None, confidence_threshold=0.9,
    )

    # Two per-call rows: one triage (salience) row + one gate (verdict) row.
    assert len(rows) == 2
    stored = store.fetch_shadow_eval(conn, model_id="cand-A")
    assert len(stored) == 2

    triage_row, gate_row = stored
    assert triage_row["api_salience"] == pytest.approx(0.9)
    assert triage_row["local_salience"] == pytest.approx(0.6)
    assert triage_row["salience_delta"] == pytest.approx(-0.3)
    assert triage_row["api_verdict"] is None
    assert triage_row["parse_ok"] == 1
    assert triage_row["latency_ms"] is not None

    assert gate_row["api_salience"] is None
    # API score 0.95 >= 0.9 -> pass; local score 0.5 < 0.9 -> reject.
    assert gate_row["api_verdict"] == "pass"
    assert gate_row["local_verdict"] == "reject"
    assert gate_row["parse_ok"] == 1
    assert gate_row["latency_ms"] is not None

    # raw_text rides along for --persist-set.
    assert all(r["raw_text"] == '{"title": "NVDA beats"}' for r in rows)


@pytest.mark.unit
def test_replay_skips_gate_for_non_promotable(tmp_path, monkeypatch):
    """Low salience / no qualifying ticker -> triage row only, no gate row."""
    db = str(tmp_path / "iic.db")
    conn = connect(db)
    raw = tmp_path / "ev2.json"
    raw.write_text("routine filler", encoding="utf-8")
    store.insert_event(
        conn, event_id="ev2", source="rss", ingested_ts=_now(), salience=0.2,
        raw_path=str(raw), status="triaged", deduped_of=None,
    )

    monkeypatch.setattr(
        "tradingagents.llm_clients.factory.create_role_llm",
        lambda role, config: _FakeClient("any", _SALIENCE_API),
    )
    cfg = {
        "llm_roles": {
            "triage_salience": {"provider": None, "model": None},
            "alert_gate": {"provider": None, "model": None},
        },
        "alert_salience_threshold": 0.85,
        "alert_ticker_confidence_threshold": 0.8,
    }
    rows = shadow_eval.replay(
        conn, cfg=cfg, model="cand-A", limit=10,
        local_base_url=None, confidence_threshold=0.9,
    )
    assert len(rows) == 1
    assert rows[0]["api_verdict"] is None and rows[0]["local_verdict"] is None


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_parse_args_defaults(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://box:8080/v1")
    args = shadow_eval.parse_args(["--model", "cand-A"])
    assert args.model == "cand-A"
    assert args.limit == 500
    assert args.report_only is False
    assert args.persist_set is False
    assert args.persist_set_path is None
    assert args.db is None
    assert args.api_p95_ms is None
    assert args.local_base_url == "http://box:8080/v1"


@pytest.mark.unit
def test_parse_args_model_required():
    with pytest.raises(SystemExit):
        shadow_eval.parse_args([])


@pytest.mark.unit
def test_default_persist_path_shape():
    path = shadow_eval.default_persist_path("qwen3.6-27b-instruct-q4_k_m")
    assert path.startswith("shadow_eval_set_")
    assert path.endswith(".json")
    assert "/" not in path  # model id is sanitized into a flat filename
