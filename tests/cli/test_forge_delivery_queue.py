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

    retry = runner.invoke(app, ["delivery", "retry", str(job_id)])
    assert retry.exit_code == 0, retry.output
    assert "requeued" in retry.output
    conn = connect(db)
    assert conn.execute(
        "SELECT state FROM delivery_queue WHERE delivery_job_id = ?",
        (job_id,),
    ).fetchone()[0] == "queued"


@pytest.mark.unit
def test_delivery_worker_command_exists():
    from cli.forge import app

    result = runner.invoke(app, ["delivery", "worker", "--help"])
    assert result.exit_code == 0
    assert "delivery worker" in result.output.lower()
