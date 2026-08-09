from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from tradingagents.delivery import queue_store
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


runner = CliRunner()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "iic.db")
    monkeypatch.setenv("TRADINGAGENTS_IIC_DB_PATH", path)
    return path


def _seed_dead_delivery(path: str) -> int:
    conn = connect(path)
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-08-08T00:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=[],
    )
    job_id = queue_store.enqueue_alert(
        conn,
        brief_id="b1",
        channel="email",
        mode="event_alert",
        brief_payload={"brief_id": "b1", "mode": "event_alert"},
        body="body",
        quiet_hours={"enabled": False},
        max_attempts=1,
        now=datetime.now(timezone.utc),
    )
    conn.execute(
        "UPDATE delivery_queue SET state = 'dead', attempt_count = 1 "
        "WHERE delivery_job_id = ?",
        (job_id,),
    )
    conn.commit()
    conn.close()
    return job_id


@pytest.mark.unit
def test_delivery_status_and_retry(db):
    from cli.forge import app

    job_id = _seed_dead_delivery(db)
    status = runner.invoke(app, ["delivery", "status"])
    assert status.exit_code == 0, status.output
    assert "dead" in status.output
    assert "email" in status.output

    retry = runner.invoke(
        app,
        ["delivery", "retry", str(job_id), "--note", "SMTP outage resolved"],
    )
    assert retry.exit_code == 0, retry.output
    assert "requeued" in retry.output
    conn = connect(db)
    assert (
        conn.execute(
            "SELECT state FROM delivery_queue WHERE delivery_job_id = ?",
            (job_id,),
        ).fetchone()[0]
        == "queued"
    )
    event = conn.execute(
        "SELECT event_type, note FROM delivery_queue_events "
        "WHERE delivery_job_id = ? ORDER BY event_id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    assert tuple(event) == ("operator_retry", "SMTP outage resolved")


@pytest.mark.unit
def test_delivery_inspect_requeue_and_cancel(db):
    from cli.forge import app

    job_id = _seed_dead_delivery(db)
    conn = connect(db)
    conn.execute(
        "UPDATE delivery_queue SET state = 'blocked', "
        "error_category = 'recipient_missing' WHERE delivery_job_id = ?",
        (job_id,),
    )
    conn.commit()
    conn.close()

    inspect = runner.invoke(app, ["delivery", "inspect", str(job_id)])
    assert inspect.exit_code == 0, inspect.output
    assert "recipient_missing" in inspect.output
    assert "brief:b1:channel:email" in inspect.output

    requeue = runner.invoke(
        app,
        [
            "delivery",
            "requeue",
            str(job_id),
            "--note",
            "recipient configured and verified",
        ],
    )
    assert requeue.exit_code == 0, requeue.output

    cancel = runner.invoke(
        app,
        [
            "delivery",
            "cancel",
            str(job_id),
            "--note",
            "cancel synthetic validation alert",
        ],
    )
    assert cancel.exit_code == 0, cancel.output
    check = connect(db)
    row = check.execute(
        "SELECT state, operator_note FROM delivery_queue WHERE delivery_job_id = ?",
        (job_id,),
    ).fetchone()
    assert tuple(row) == ("cancelled", "cancel synthetic validation alert")


@pytest.mark.unit
def test_delivery_retry_requires_operator_note(db):
    from cli.forge import app

    job_id = _seed_dead_delivery(db)
    retry = runner.invoke(app, ["delivery", "retry", str(job_id)])
    assert retry.exit_code != 0


@pytest.mark.unit
def test_delivery_worker_command_exists():
    from cli.forge import app

    result = runner.invoke(app, ["delivery", "worker", "--help"])
    assert result.exit_code == 0
    assert "delivery worker" in result.output.lower()
