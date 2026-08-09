import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def setup(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    raw = tmp_path / "data" / "events" / "ev1.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps({"text": "trigger event text"}))
    store.insert_event(conn, event_id="ev1", source="rss",
                       ingested_ts=_now(), salience=0.9,
                       raw_path=str(raw),
                       status="triaged", deduped_of=None)
    return conn, str(tmp_path / "data")


@pytest.mark.unit
def test_dispatch_event_alert_calls_secretary_with_payload(setup):
    from tradingagents.orchestrator.dispatch import dispatch_event_alert

    conn, data_dir = setup
    sec = MagicMock()
    sec.compose_event_alert.return_value = "b1"

    # Seed brief row so the dispatch_event_alert post-call lookup finds run_ids
    store.insert_brief(conn, brief_id="b1", mode="event_alert",
                       scope="AAPL", generated_ts=_now(),
                       content_path="briefs/b1.md",
                       run_ids=[], parent_brief_id=None,
                       trigger_event_id="ev1")

    job = {
        "job_id": 1,
        "job_type": "event_alert",
        "payload": json.dumps({"event_id": "ev1", "ticker": "AAPL"}),
        "trigger_event_id": "ev1",
    }
    result = dispatch_event_alert(conn, job, secretary=sec)

    sec.compose_event_alert.assert_called_once_with(
        event_id="ev1", ticker="AAPL", job_id=1, parent_brief_id=None,
        deliver=True,
    )
    assert result["brief_id"] == "b1"
    assert result["run_ids"] == []
    assert result["cost_usd"] == 0.0


@pytest.mark.unit
def test_dispatch_event_alert_cost_rollup(setup):
    from tradingagents.orchestrator.dispatch import dispatch_event_alert

    conn, data_dir = setup
    sec = MagicMock()
    sec.compose_event_alert.return_value = "b1"

    # Seed a queue_jobs row so runs.queue_job_id FK resolves.
    cur = conn.execute(
        "INSERT INTO queue_jobs (job_type, payload, state, enqueued_ts) "
        "VALUES ('event_alert', '{}', 'running', ?)",
        (_now(),),
    )
    job_id = cur.lastrowid
    conn.commit()

    # Seed two run rows + two costs rows with known dollar values
    for rid in ("r1", "r2"):
        store.insert_run(conn, run_id=rid, ticker="AAPL", persona_id="macro",
                         started_ts=_now(), artifact_dir=f"runs/{rid}",
                         queue_job_id=job_id)
        store.finalize_run(conn, run_id=rid, ended_ts=_now(),
                            status="complete", decision="BUY", confidence=None)
        store.record_cost(conn, run_id=rid, provider="deepseek",
                          model="m", in_tokens=100, out_tokens=50,
                          usd_estimate=0.25)
    # Also seed a brief row with run_ids so dispatch can find them
    store.insert_brief(conn, brief_id="b1", mode="event_alert",
                       scope="AAPL", generated_ts=_now(),
                       content_path="briefs/b1.md",
                       run_ids=["r1", "r2"], parent_brief_id=None,
                       trigger_event_id="ev1")

    job = {
        "job_id": job_id,
        "job_type": "event_alert",
        "payload": json.dumps({"event_id": "ev1", "ticker": "AAPL"}),
        "trigger_event_id": "ev1",
    }
    result = dispatch_event_alert(conn, job, secretary=sec)
    assert result["cost_usd"] == pytest.approx(0.50)
    assert sorted(result["run_ids"]) == ["r1", "r2"]


@pytest.mark.unit
def test_dispatch_unknown_job_type_raises(setup):
    from tradingagents.orchestrator.dispatch import JobBlockedError, dispatch

    conn, data_dir = setup
    sec = MagicMock()
    job = {"job_id": 1, "job_type": "portfolio_rebalance", "payload": "{}",
           "trigger_event_id": None}
    with pytest.raises(JobBlockedError, match="unknown job_type") as exc_info:
        dispatch(conn, job, secretary=sec)
    assert exc_info.value.category == "unknown_job_type"


@pytest.mark.unit
def test_dispatch_morning_digest_uses_stable_payload_and_rolls_up(setup):
    from datetime import date
    from tradingagents.orchestrator.dispatch import dispatch_morning_digest
    from tradingagents.runtime.scheduler import morning_digest_brief_id

    conn, _data_dir = setup
    brief_id = morning_digest_brief_id(date(2026, 8, 9))
    store.insert_brief(
        conn,
        brief_id=brief_id,
        mode="morning_digest",
        scope='["AAPL"]',
        generated_ts="2026-08-09T07:00:00+08:00",
        content_path=f"briefs/{brief_id}.md",
        run_ids=[],
    )
    secretary = MagicMock()
    secretary.compose_morning_digest.return_value = brief_id
    job = {
        "job_id": 2,
        "job_type": "morning_digest",
        "payload": json.dumps(
            {
                "brief_id": brief_id,
                "local_date": "2026-08-09",
                "scheduled_ts": "2026-08-09T07:00:00+08:00",
            }
        ),
        "trigger_event_id": None,
    }

    result = dispatch_morning_digest(conn, job, secretary=secretary)

    secretary.compose_morning_digest.assert_called_once_with(
        watchlist=None,
        ts="2026-08-09T07:00:00+08:00",
        brief_id=brief_id,
        deliver=True,
    )
    assert result == {"brief_id": brief_id, "run_ids": [], "cost_usd": 0.0}


@pytest.mark.unit
def test_dispatch_morning_digest_rejects_mismatched_daily_id(setup):
    from tradingagents.orchestrator.dispatch import JobBlockedError, dispatch

    conn, _data_dir = setup
    job = {
        "job_id": 2,
        "job_type": "morning_digest",
        "payload": json.dumps(
            {
                "brief_id": "wrong",
                "local_date": "2026-08-09",
                "scheduled_ts": "2026-08-09T07:00:00+08:00",
            }
        ),
        "trigger_event_id": None,
    }
    with pytest.raises(JobBlockedError, match="deterministic daily id") as exc_info:
        dispatch(conn, job, secretary=MagicMock())
    assert exc_info.value.category == "invalid_payload"


@pytest.mark.unit
@pytest.mark.parametrize("payload", ["not-json", "[]", "{}"])
def test_dispatch_invalid_payload_is_permanently_classified(setup, payload):
    from tradingagents.orchestrator.dispatch import JobBlockedError, dispatch

    conn, _data_dir = setup
    job = {
        "job_id": 1,
        "job_type": "event_alert",
        "payload": payload,
        "trigger_event_id": "ev1",
    }
    with pytest.raises(JobBlockedError) as exc_info:
        dispatch(conn, job, secretary=MagicMock())
    assert exc_info.value.category == "invalid_payload"


@pytest.mark.unit
def test_dispatch_missing_event_is_permanently_classified(setup):
    from tradingagents.orchestrator.dispatch import JobBlockedError, dispatch

    conn, _data_dir = setup
    job = {
        "job_id": 1,
        "job_type": "event_alert",
        "payload": json.dumps({"event_id": "gone", "ticker": "AAPL"}),
        "trigger_event_id": "gone",
    }
    with pytest.raises(JobBlockedError) as exc_info:
        dispatch(conn, job, secretary=MagicMock())
    assert exc_info.value.category == "missing_event"


@pytest.mark.unit
def test_dispatch_event_alert_links_parent_light_brief(tmp_path):
    import json
    from unittest.mock import MagicMock
    from tradingagents.persistence.db import connect
    from tradingagents.persistence import store
    from tradingagents.orchestrator.dispatch import dispatch_event_alert

    conn = connect(str(tmp_path / "iic.db"))
    store.insert_event(conn, event_id="ev1", source="rss",
                       ingested_ts="2026-06-01T00:00:00+00:00", salience=0.9,
                       raw_path=None, status="triaged", deduped_of=None)
    # a light brief already exists for this event
    store.insert_brief(conn, brief_id="lb1", mode="event_alert_light",
                       scope='["NVDA"]', generated_ts="2026-06-01T00:00:00+00:00",
                       content_path="briefs/lb1.md", run_ids=[],
                       trigger_event_id="ev1")
    # full brief the secretary is mocked to produce
    store.insert_brief(conn, brief_id="fb1", mode="event_alert", scope="NVDA",
                       generated_ts="2026-06-01T00:10:00+00:00",
                       content_path="briefs/fb1.md", run_ids=["r1"],
                       parent_brief_id="lb1", trigger_event_id="ev1")
    sec = MagicMock()
    sec.compose_event_alert.return_value = "fb1"

    job = {"job_id": 1, "payload": json.dumps({"event_id": "ev1", "ticker": "NVDA"})}
    dispatch_event_alert(conn, job, secretary=sec)

    _, kwargs = sec.compose_event_alert.call_args
    assert kwargs["parent_brief_id"] == "lb1"
    assert kwargs["deliver"] is True
