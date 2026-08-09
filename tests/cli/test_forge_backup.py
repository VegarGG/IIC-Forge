from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tradingagents.persistence.db import connect


runner = CliRunner()


def _backup_fixture(tmp_path: Path):
    data = tmp_path / "data"
    redis = tmp_path / "redis"
    output = tmp_path / "backups"
    data.mkdir()
    (redis / "appendonlydir").mkdir(parents=True)
    output.mkdir()
    connect(str(data / "iic.db")).close()
    (redis / "appendonlydir" / "appendonly.aof.manifest").write_text(
        "file appendonly.aof.1.base.rdb seq 1 type b\n", encoding="utf-8"
    )
    (redis / "appendonlydir" / "appendonly.aof.1.base.rdb").write_bytes(
        b"REDIS0012"
    )
    key = tmp_path / "key"
    key.write_bytes(base64.b64encode(os.urandom(32)) + b"\n")
    return data, redis, output, key


@pytest.mark.unit
def test_backup_cli_create_verify_and_status(tmp_path):
    from cli.forge import app

    data, redis, output, key = _backup_fixture(tmp_path)
    created = runner.invoke(
        app,
        [
            "backup",
            "create",
            "--data-root",
            str(data),
            "--redis-root",
            str(redis),
            "--output-root",
            str(output),
            "--key-file",
            str(key),
        ],
    )
    assert created.exit_code == 0, created.output
    archive = next(output.glob("*.iicbak"))

    verified = runner.invoke(
        app, ["backup", "verify", str(archive), "--key-file", str(key)]
    )
    assert verified.exit_code == 0, verified.output
    assert '"status": "verified"' in verified.output

    status = runner.invoke(
        app, ["backup", "status", "--output-root", str(output)]
    )
    assert status.exit_code == 0, status.output
    assert '"status": "current"' in status.output


@pytest.mark.unit
def test_backup_cli_restore_refuses_missing_confirmation(tmp_path):
    from cli.forge import app

    data, redis, output, key = _backup_fixture(tmp_path)
    created = runner.invoke(
        app,
        [
            "backup",
            "create",
            "--data-root",
            str(data),
            "--redis-root",
            str(redis),
            "--output-root",
            str(output),
            "--key-file",
            str(key),
        ],
    )
    assert created.exit_code == 0, created.output
    archive = next(output.glob("*.iicbak"))
    target_data = tmp_path / "target-data"
    target_redis = tmp_path / "target-redis"
    target_data.mkdir()
    target_redis.mkdir()
    refused = runner.invoke(
        app,
        [
            "backup",
            "restore",
            str(archive),
            "--data-root",
            str(target_data),
            "--redis-root",
            str(target_redis),
            "--key-file",
            str(key),
            "--confirm",
            "wrong",
        ],
    )
    assert refused.exit_code != 0
    assert "exact confirmation" in refused.output
