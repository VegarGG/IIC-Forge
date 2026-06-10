"""Task 10 telemetry tests: model_id, parse_ok, latency_ms on alert_evaluations.

Tests cover:
- evaluate_alert_candidate with a valid JSON response → parse_ok=True, latency_ms non-null, model_id recorded
- evaluate_alert_candidate with malformed JSON → parse_ok=False, latency_ms non-null, model_id still recorded
- fetch_alert_eval_telemetry query helper returns expected rows
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tradingagents.orchestrator.alert_evaluator import (
    AlertEvaluationPayload,
    alert_evaluation_response_format,
    evaluate_alert_candidate,
)
from tradingagents.persistence.db import connect
from tradingagents.persistence.store import (
    fetch_alert_eval_telemetry,
    insert_alert_evaluation,
    insert_event,
)


_VALID_JSON = (
    '{"decision":"pass","score":0.91,"materiality":"earnings surprise",'
    '"actionability":"watchlist thesis may change",'
    '"ticker_link_evidence":"NVDA named directly","novelty":"new filing",'
    '"disqualifiers":[],"reasons":["direct and material"]}'
)

_MALFORMED_JSON = "not valid json at all {{{"


class FakeLLM:
    """Minimal fake LLM — records the last prompt, returns a fixed response."""

    def __init__(self, content: str, model_name: str = "test-model-v1"):
        self._content = content
        self.model_name = model_name
        self.last_prompt: str | None = None

    def invoke(self, prompt):
        self.last_prompt = prompt
        return SimpleNamespace(content=self._content)


# ---------------------------------------------------------------------------
# evaluate_alert_candidate — telemetry fields on the returned AlertEvaluation
# ---------------------------------------------------------------------------

class TestEvaluateAlertCandidateTelemetry:
    def test_valid_json_parse_ok_true(self):
        llm = FakeLLM(_VALID_JSON)
        result = evaluate_alert_candidate(
            llm=llm,
            event_text="NVDA raises guidance after earnings.",
            tickers=["NVDA"],
            min_score=0.80,
        )
        assert result.parse_ok is True
        assert result.latency_ms is not None
        assert isinstance(result.latency_ms, int)
        assert result.latency_ms >= 0
        assert result.model_id == "test-model-v1"

    def test_malformed_json_parse_ok_false(self):
        llm = FakeLLM(_MALFORMED_JSON)
        result = evaluate_alert_candidate(
            llm=llm,
            event_text="generic market chatter",
            tickers=["AAPL"],
            min_score=0.80,
        )
        assert result.parse_ok is False
        assert result.latency_ms is not None
        assert isinstance(result.latency_ms, int)
        assert result.latency_ms >= 0
        assert result.model_id == "test-model-v1"
        # Existing failure behavior preserved
        assert result.passed is False
        assert "invalid_json" in result.disqualifiers

    def test_model_id_from_optional_param_overrides_llm_attr(self):
        """Explicit model_id kwarg takes priority over llm.model_name."""
        llm = FakeLLM(_VALID_JSON, model_name="llm-attr-name")
        result = evaluate_alert_candidate(
            llm=llm,
            event_text="NVDA raises guidance.",
            tickers=["NVDA"],
            min_score=0.80,
            model_id="explicit-override-id",
        )
        assert result.model_id == "explicit-override-id"

    def test_model_id_falls_back_to_llm_model_name(self):
        llm = FakeLLM(_VALID_JSON, model_name="deepseek-chat")
        result = evaluate_alert_candidate(
            llm=llm,
            event_text="NVDA raises guidance.",
            tickers=["NVDA"],
            min_score=0.80,
        )
        assert result.model_id == "deepseek-chat"

    def test_model_id_none_when_llm_lacks_attr(self):
        """LLM objects without model_name produce model_id=None (not an error)."""
        class BareLLM:
            def invoke(self, prompt):
                return SimpleNamespace(content=_VALID_JSON)

        result = evaluate_alert_candidate(
            llm=BareLLM(),
            event_text="NVDA raises guidance.",
            tickers=["NVDA"],
            min_score=0.80,
        )
        assert result.model_id is None
        assert result.parse_ok is True


# ---------------------------------------------------------------------------
# DB round-trip: insert_alert_evaluation with new columns
# ---------------------------------------------------------------------------

class TestAlertEvaluationDbColumns:
    @pytest.fixture
    def conn(self, tmp_path):
        db_path = str(tmp_path / "iic.db")
        c = connect(db_path)
        # seed a parent event row (FK constraint)
        insert_event(
            c,
            event_id="evt-001",
            source="polygon_news",
            ingested_ts="2025-01-01T00:00:00+00:00",
            salience=0.9,
            raw_path=None,
            status="triaged",
            deduped_of=None,
        )
        return c

    def test_insert_with_telemetry_columns(self, conn):
        row_id = insert_alert_evaluation(
            conn,
            event_id="evt-001",
            tickers=["NVDA"],
            decision="pass",
            score=0.91,
            payload={"decision": "pass", "score": 0.91},
            created_ts="2025-01-01T00:01:00+00:00",
            model_id="test-model-v1",
            parse_ok=True,
            latency_ms=123,
        )
        assert row_id is not None

        row = conn.execute(
            "SELECT * FROM alert_evaluations WHERE evaluation_id = ?", (row_id,)
        ).fetchone()
        assert row["model_id"] == "test-model-v1"
        assert bool(row["parse_ok"]) is True
        assert row["latency_ms"] == 123

    def test_insert_with_parse_ok_false(self, conn):
        row_id = insert_alert_evaluation(
            conn,
            event_id="evt-001",
            tickers=["AAPL"],
            decision="reject",
            score=0.0,
            payload={"decision": "reject", "score": 0.0},
            created_ts="2025-01-01T00:02:00+00:00",
            model_id="test-model-v1",
            parse_ok=False,
            latency_ms=55,
        )
        row = conn.execute(
            "SELECT * FROM alert_evaluations WHERE evaluation_id = ?", (row_id,)
        ).fetchone()
        assert bool(row["parse_ok"]) is False
        assert row["latency_ms"] == 55

    def test_insert_without_new_columns_still_works(self, conn):
        """Callers that don't yet pass model_id/parse_ok/latency_ms default to NULL."""
        row_id = insert_alert_evaluation(
            conn,
            event_id="evt-001",
            tickers=["TSLA"],
            decision="reject",
            score=0.3,
            payload={"decision": "reject", "score": 0.3},
            created_ts="2025-01-01T00:03:00+00:00",
        )
        row = conn.execute(
            "SELECT * FROM alert_evaluations WHERE evaluation_id = ?", (row_id,)
        ).fetchone()
        assert row["model_id"] is None
        assert row["parse_ok"] is None
        assert row["latency_ms"] is None


# ---------------------------------------------------------------------------
# fetch_alert_eval_telemetry
# ---------------------------------------------------------------------------

class TestFetchAlertEvalTelemetry:
    @pytest.fixture
    def conn_with_rows(self, tmp_path):
        db_path = str(tmp_path / "iic.db")
        c = connect(db_path)
        insert_event(
            c,
            event_id="evt-telem",
            source="polygon_news",
            ingested_ts="2025-01-01T00:00:00+00:00",
            salience=0.85,
            raw_path=None,
            status="triaged",
            deduped_of=None,
        )
        # Row 1: parse_ok=True, pass
        insert_alert_evaluation(
            c,
            event_id="evt-telem",
            tickers=["NVDA"],
            decision="pass",
            score=0.91,
            payload={},
            created_ts="2025-01-02T00:00:00+00:00",
            model_id="deepseek-chat",
            parse_ok=True,
            latency_ms=200,
        )
        # Row 2: parse_ok=False (parse failure reject)
        insert_alert_evaluation(
            c,
            event_id="evt-telem",
            tickers=["AAPL"],
            decision="reject",
            score=0.0,
            payload={},
            created_ts="2025-01-02T00:01:00+00:00",
            model_id="deepseek-chat",
            parse_ok=False,
            latency_ms=50,
        )
        # Row 3: parse_ok=True, reject (genuine low score)
        insert_alert_evaluation(
            c,
            event_id="evt-telem",
            tickers=["TSLA"],
            decision="reject",
            score=0.4,
            payload={},
            created_ts="2025-01-02T00:02:00+00:00",
            model_id="deepseek-chat",
            parse_ok=True,
            latency_ms=175,
        )
        return c

    def test_returns_all_rows(self, conn_with_rows):
        rows = fetch_alert_eval_telemetry(conn_with_rows)
        assert len(rows) == 3

    def test_row_has_required_columns(self, conn_with_rows):
        rows = fetch_alert_eval_telemetry(conn_with_rows)
        for r in rows:
            assert "model_id" in r
            assert "parse_ok" in r
            assert "latency_ms" in r
            assert "decision" in r
            assert "created_ts" in r

    def test_parse_ok_false_count(self, conn_with_rows):
        rows = fetch_alert_eval_telemetry(conn_with_rows)
        parse_failures = [r for r in rows if r["parse_ok"] == 0]
        assert len(parse_failures) == 1

    def test_filter_by_model_id(self, conn_with_rows):
        rows = fetch_alert_eval_telemetry(conn_with_rows, model_id="deepseek-chat")
        assert len(rows) == 3

        rows_none = fetch_alert_eval_telemetry(conn_with_rows, model_id="nonexistent")
        assert len(rows_none) == 0

    def test_latency_values_preserved(self, conn_with_rows):
        rows = fetch_alert_eval_telemetry(conn_with_rows)
        latencies = sorted(r["latency_ms"] for r in rows)
        assert latencies == [50, 175, 200]


# ---------------------------------------------------------------------------
# alert_evaluation_response_format helper
# ---------------------------------------------------------------------------

class TestAlertEvaluationResponseFormat:
    def test_returns_dict_with_json_schema_type(self):
        fmt = alert_evaluation_response_format()
        assert fmt["type"] == "json_schema"
        assert "json_schema" in fmt
        assert fmt["json_schema"]["strict"] is False

    def test_schema_has_required_fields(self):
        fmt = alert_evaluation_response_format()
        schema = fmt["json_schema"]["schema"]
        props = schema.get("properties", {})
        assert "decision" in props
        assert "score" in props
        assert "materiality" in props
        assert "actionability" in props

    def test_schema_no_minimum_maximum_keys(self):
        """Ensure score field_validator prevents minimum/maximum leaking into schema."""
        fmt = alert_evaluation_response_format()
        schema_str = json.dumps(fmt["json_schema"]["schema"])
        assert "minimum" not in schema_str
        assert "maximum" not in schema_str
