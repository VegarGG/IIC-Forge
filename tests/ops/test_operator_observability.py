from __future__ import annotations

import base64
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest


def _config(tmp_path):
    return {
        "iic_db_path": str(tmp_path / "iic.db"),
        "iic_data_dir": str(tmp_path / "data"),
        "sensing_redis_url": "redis://127.0.0.1:6379/0",
        "daily_budget_timezone": "Asia/Shanghai",
        "daily_budget_usd": 20.0,
        "delivery": {
            "enabled_channels": ["telegram", "email"],
            "quiet_hours": {
                "enabled": False,
                "start": "22:00",
                "end": "07:00",
                "timezone": "Asia/Shanghai",
            },
            "queue_max_attempts": 5,
        },
    }


@pytest.mark.unit
def test_operational_alert_is_durable_and_deduplicated(tmp_path):
    from tradingagents.ops.alerts import (
        record_operational_alert,
        resolve_absent_alerts,
    )
    from tradingagents.persistence.db import connect

    cfg = _config(tmp_path)
    conn = connect(cfg["iic_db_path"])
    first_id, created = record_operational_alert(
        conn,
        config=cfg,
        dedup_key="redis:health",
        category="redis",
        severity="critical",
        summary="Redis health check failed",
        details={"error": "ConnectionError"},
    )
    second_id, repeated_created = record_operational_alert(
        conn,
        config=cfg,
        dedup_key="redis:health",
        category="redis",
        severity="critical",
        summary="Redis health check failed",
    )

    assert created is True and repeated_created is False
    assert first_id == second_id
    alert = conn.execute(
        "SELECT occurrence_count, state, delivery_brief_id FROM operational_alerts"
    ).fetchone()
    assert (alert["occurrence_count"], alert["state"]) == (2, "open")
    jobs = list(
        conn.execute(
            "SELECT channel, mode, state, attempt_count FROM delivery_queue "
            "ORDER BY channel"
        )
    )
    assert [tuple(job) for job in jobs] == [
        ("email", "operational_alert", "queued", 0),
        ("telegram", "operational_alert", "queued", 0),
    ]
    assert resolve_absent_alerts(conn, active_dedup_keys=set()) == 1
    reopened_id, requeued = record_operational_alert(
        conn,
        config=cfg,
        dedup_key="redis:health",
        category="redis",
        severity="critical",
        summary="Redis health check failed again",
    )
    assert reopened_id == first_id and requeued is True
    assert conn.execute("SELECT COUNT(*) FROM delivery_queue").fetchone()[0] == 4
    conn.close()


@pytest.mark.unit
def test_service_heartbeat_is_real_process_liveness(tmp_path):
    from tradingagents.ops.heartbeat import write_heartbeat
    from tradingagents.persistence.db import connect

    path = str(tmp_path / "iic.db")
    connect(path).close()
    write_heartbeat(
        path,
        service_name="analysis-worker",
        instance_id="instance-1",
        started_ts="2026-08-09T00:00:00+00:00",
        success=True,
        detail={"token": "must-not-be-a-credential-value"},
    )
    conn = connect(path)
    row = conn.execute(
        "SELECT service_name, instance_id, status, success_ts, detail "
        "FROM service_heartbeats"
    ).fetchone()
    assert tuple(row[:3]) == ("analysis-worker", "instance-1", "active")
    assert row["success_ts"]
    assert json.loads(row["detail"])["token"] == "[REDACTED]"
    conn.close()


@pytest.mark.unit
def test_structured_logging_redacts_secrets(monkeypatch):
    from tradingagents.ops.logging import JsonFormatter

    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-value")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter(service_name="test-service"))
    logger = logging.getLogger("operator-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "request token=abc123 key super-secret-value",
        extra={"correlation_id": "analysis-job:9"},
    )
    payload = json.loads(output.getvalue())
    assert payload["service"] == "test-service"
    assert payload["correlation_id"] == "analysis-job:9"
    assert "super-secret-value" not in output.getvalue()
    assert "abc123" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()


@pytest.mark.unit
def test_budget_release_is_append_only_and_single_use(tmp_path):
    from tradingagents.llm_clients.daily_budget import (
        daily_budget_total,
        release_stale_reservation,
    )
    from tradingagents.persistence.db import connect

    conn = connect(str(tmp_path / "iic.db"))
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    created = (now - timedelta(hours=2)).isoformat()
    conn.execute(
        "INSERT INTO llm_budget_ledger (call_id, budget_date, budget_timezone, "
        "provider, model, state, reserved_usd, created_ts) VALUES "
        "('abandoned', '2026-08-09', 'Asia/Shanghai', 'deepseek', 'model', "
        "'reserved', 1.0, ?)",
        (created,),
    )
    conn.commit()
    assert daily_budget_total(conn, budget_date="2026-08-09") == 1.0

    released = release_stale_reservation(
        conn,
        call_id="abandoned",
        operator_note="worker crashed before provider call",
        evidence="provider request id absent from gateway logs",
        confirm="RELEASE abandoned",
        now=now,
    )
    assert released == 1.0
    assert daily_budget_total(conn, budget_date="2026-08-09") == 0.0
    assert conn.execute(
        "SELECT state FROM llm_budget_ledger WHERE call_id='abandoned'"
    ).fetchone()[0] == "reserved"
    with pytest.raises(ValueError, match="already released"):
        release_stale_reservation(
            conn,
            call_id="abandoned",
            operator_note="repeat",
            evidence="repeat",
            confirm="RELEASE abandoned",
            now=now,
        )
    conn.close()


@pytest.mark.unit
def test_status_excludes_queue_payloads_and_flags_failures(tmp_path):
    from tradingagents.ops.status import collect_status, health_issues
    from tradingagents.persistence.db import connect

    cfg = _config(tmp_path)
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    data.mkdir()
    backups.mkdir()
    conn = connect(cfg["iic_db_path"])
    conn.execute(
        "INSERT INTO queue_jobs (job_type, payload, state, enqueued_ts, error_category) "
        "VALUES ('test', ?, 'error', ?, 'runtime_error')",
        ('{"secret":"do-not-expose"}', datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    status = collect_status(
        conn, cfg, backup_root=backups, redis_check=False
    )
    encoded = json.dumps(status)
    assert "do-not-expose" not in encoded
    assert status["queues"]["analysis"]["counts"]["error"] == 1
    issues = health_issues(status, require_all_services=False)
    assert any(issue["dedup_key"] == "analysis-queue:error" for issue in issues)
    conn.close()


@pytest.mark.unit
def test_retention_is_preview_first_and_requires_exact_confirmation(tmp_path):
    from tradingagents.ops.retention import apply_retention, retention_candidates
    from tradingagents.persistence.db import connect

    data = tmp_path / "data"
    events = data / "events"
    quarantine = events / "quarantine"
    quarantine.mkdir(parents=True)
    raw = quarantine / "old.json"
    raw.write_text("{}", encoding="utf-8")
    conn = connect(str(data / "iic.db"))
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    conn.execute(
        "INSERT INTO ingest_quarantine (quarantine_id, source, observed_ts, "
        "reason_codes, warning_codes, raw_path, envelope_sha256, details) "
        "VALUES ('q1', 'rss', ?, '[]', '[]', ?, ?, '{}')",
        (old, str(raw), "a" * 64),
    )
    conn.commit()
    preview = retention_candidates(conn, data_dir=data)
    assert preview["quarantine_ids"] == ["q1"]
    assert raw.is_file()
    with pytest.raises(ValueError, match="confirmation"):
        apply_retention(
            conn, candidates=preview, operator_note="approved cleanup", confirm="wrong"
        )
    result = apply_retention(
        conn,
        candidates=preview,
        operator_note="approved cleanup",
        confirm="APPLY IIC-FORGE RETENTION",
    )
    assert result["quarantine_removed"] == 1
    assert not raw.exists()
    conn.close()


@pytest.mark.unit
def test_restore_drill_performs_disposable_restore_and_records_evidence(tmp_path):
    from tradingagents.backup import create_backup
    from tradingagents.ops.recovery import run_restore_drill
    from tradingagents.persistence.db import connect

    data = tmp_path / "data"
    redis = tmp_path / "redis"
    output = tmp_path / "backups"
    data.mkdir()
    output.mkdir()
    appendonly = redis / "appendonlydir"
    appendonly.mkdir(parents=True)
    (appendonly / "appendonly.aof.manifest").write_text(
        "file appendonly.aof.1.base.rdb seq 1 type b\n", encoding="utf-8"
    )
    (appendonly / "appendonly.aof.1.base.rdb").write_bytes(b"REDIS0012")
    key = tmp_path / "key"
    key.write_bytes(base64.b64encode(os.urandom(32)) + b"\n")
    conn = connect(str(data / "iic.db"))
    created = create_backup(
        data_root=data,
        redis_root=redis,
        output_root=output,
        key_file=key,
        apply_retention=False,
    )
    result = run_restore_drill(
        conn,
        archive=created["path"],
        key_file=key,
        operator_note="automated disposable drill",
        confirm="RUN RESTORE DRILL",
    )
    assert result["status"] == "passed"
    assert conn.execute(
        "SELECT status FROM recovery_drills ORDER BY drill_id DESC LIMIT 1"
    ).fetchone()[0] == "passed"
    conn.close()
