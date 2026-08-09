import json

import pytest
from typer.testing import CliRunner


runner = CliRunner()


@pytest.fixture
def operator_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    db_path = data / "iic.db"
    monkeypatch.setenv("TRADINGAGENTS_IIC_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADINGAGENTS_IIC_DATA_DIR", str(data))
    monkeypatch.setenv("IIC_BACKUP_DIR", str(backups))
    from tradingagents.persistence.db import connect

    connect(str(db_path)).close()
    return db_path


@pytest.mark.unit
def test_operator_status_is_machine_readable_and_omits_payload(operator_env):
    from cli.forge import app
    from tradingagents.persistence.db import connect

    conn = connect(str(operator_env))
    conn.execute(
        "INSERT INTO queue_jobs (job_type, payload, state, enqueued_ts) "
        "VALUES ('test', '{\"secret\":\"never-print\"}', 'queued', "
        "'2026-08-09T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["operator", "status", "--no-redis"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["database"]["status"] == "ok"
    assert payload["queues"]["analysis"]["counts"]["queued"] == 1
    assert "never-print" not in result.output


@pytest.mark.unit
def test_orchestrator_inspect_and_cancel_are_audited(operator_env):
    from cli.forge import app
    from tradingagents.orchestrator.queue_store import insert_queue_job
    from tradingagents.persistence.db import connect

    conn = connect(str(operator_env))
    job_id = insert_queue_job(
        conn,
        job_type="test",
        payload='{"secret":"not-diagnostic"}',
        trigger_event_id=None,
    )
    conn.close()
    inspected = runner.invoke(app, ["orchestrator", "inspect", str(job_id)])
    assert inspected.exit_code == 0, inspected.output
    assert "not-diagnostic" not in inspected.output

    cancelled = runner.invoke(
        app,
        ["orchestrator", "cancel", str(job_id), "--note", "obsolete test work"],
    )
    assert cancelled.exit_code == 0, cancelled.output
    verify = connect(str(operator_env))
    assert verify.execute(
        "SELECT state FROM queue_jobs WHERE job_id=?", (job_id,)
    ).fetchone()[0] == "cancelled"
    action = verify.execute(
        "SELECT action_type, result FROM operator_actions WHERE target_id=?",
        (str(job_id),),
    ).fetchone()
    assert tuple(action) == ("cancel", "cancelled")
    verify.close()
