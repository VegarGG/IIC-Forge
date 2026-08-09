from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingagents.persistence.db import connect


def _key(path: Path) -> Path:
    path.write_bytes(base64.b64encode(os.urandom(32)) + b"\n")
    path.chmod(0o600)
    return path


def _source_roots(tmp_path: Path, *, ticker: str = "AAPL"):
    data = tmp_path / "data"
    redis = tmp_path / "redis"
    output = tmp_path / "backups"
    data.mkdir()
    (redis / "appendonlydir").mkdir(parents=True)
    output.mkdir()
    conn = connect(str(data / "iic.db"))
    conn.execute(
        "INSERT INTO watchlist (ticker, added_ts, tags) VALUES (?, ?, '[]')",
        (ticker, "2026-08-09T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    (data / "events").mkdir()
    (data / "events" / "payload.json").write_text(
        '{"private":"backup-canary"}', encoding="utf-8"
    )
    (redis / "appendonlydir" / "appendonly.aof.manifest").write_text(
        "file appendonly.aof.1.base.rdb seq 1 type b\n", encoding="utf-8"
    )
    (redis / "appendonlydir" / "appendonly.aof.1.base.rdb").write_bytes(
        b"REDIS0012-backup-canary"
    )
    return data, redis, output


@pytest.mark.unit
def test_create_verify_status_and_restore_round_trip(tmp_path):
    from tradingagents.backup.archive import (
        CONFIRM_RESTORE,
        backup_status,
        create_backup,
        restore_backup,
        verify_backup,
    )

    data, redis, output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")
    result = create_backup(
        data_root=data,
        redis_root=redis,
        output_root=output,
        key_file=key,
        now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
    )
    archive = Path(result["path"])
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.read_bytes().startswith(b"IICBKP01")
    assert b"backup-canary" not in archive.read_bytes()
    assert Path(f"{archive}.sha256").is_file()
    assert result["verification"] == "verified"
    assert verify_backup(archive, key_file=key)["status"] == "verified"
    assert backup_status(
        output,
        now=datetime(2026, 8, 9, 12, 30, tzinfo=timezone.utc),
    )["status"] == "current"

    restored_data = tmp_path / "restored-data"
    restored_redis = tmp_path / "restored-redis"
    restored_data.mkdir()
    restored_redis.mkdir()
    restored = restore_backup(
        archive,
        key_file=key,
        data_root=restored_data,
        redis_root=restored_redis,
        confirm=CONFIRM_RESTORE,
    )
    assert restored["status"] == "restored"
    conn = connect(str(restored_data / "iic.db"))
    assert conn.execute("SELECT ticker FROM watchlist").fetchone()[0] == "AAPL"
    conn.close()
    assert (
        restored_redis / "appendonlydir" / "appendonly.aof.manifest"
    ).is_file()


@pytest.mark.unit
def test_wrong_key_and_ciphertext_tamper_fail_authentication(tmp_path):
    from tradingagents.backup.archive import BackupError, create_backup, verify_backup

    data, redis, output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")
    result = create_backup(
        data_root=data, redis_root=redis, output_root=output, key_file=key
    )
    archive = Path(result["path"])
    wrong_key = _key(tmp_path / "wrong.key")
    with pytest.raises(BackupError, match="authentication failed"):
        verify_backup(archive, key_file=wrong_key)

    content = bytearray(archive.read_bytes())
    content[len(content) // 2] ^= 0x01
    archive.write_bytes(content)
    with pytest.raises(BackupError, match="checksum does not match"):
        verify_backup(archive, key_file=key)


@pytest.mark.unit
def test_restore_requires_confirmation_before_mutating_targets(tmp_path):
    from tradingagents.backup.archive import BackupError, create_backup, restore_backup

    data, redis, output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")
    archive = create_backup(
        data_root=data, redis_root=redis, output_root=output, key_file=key
    )["path"]
    target_data = tmp_path / "target-data"
    target_redis = tmp_path / "target-redis"
    target_data.mkdir()
    target_redis.mkdir()
    (target_data / "sentinel").write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError, match="exact confirmation"):
        restore_backup(
            archive,
            key_file=key,
            data_root=target_data,
            redis_root=target_redis,
            confirm="no",
        )
    assert (target_data / "sentinel").read_text(encoding="utf-8") == "keep"


@pytest.mark.unit
def test_redis_swap_failure_rolls_back_already_swapped_data(tmp_path, monkeypatch):
    import tradingagents.backup.archive as module

    data, redis, output = _source_roots(tmp_path, ticker="BACKUP")
    key = _key(tmp_path / "backup.key")
    archive = module.create_backup(
        data_root=data, redis_root=redis, output_root=output, key_file=key
    )["path"]

    target_data = tmp_path / "target-data"
    target_redis = tmp_path / "target-redis"
    target_data.mkdir()
    (target_redis / "appendonlydir").mkdir(parents=True)
    conn = connect(str(target_data / "iic.db"))
    conn.execute(
        "INSERT INTO watchlist (ticker, added_ts, tags) VALUES "
        "('ORIGINAL', '2026-08-09T00:00:00+00:00', '[]')"
    )
    conn.commit()
    conn.close()
    (target_redis / "appendonlydir" / "appendonly.aof.manifest").write_text(
        "old", encoding="utf-8"
    )

    original_swap = module._swap_root
    calls = 0

    def fail_second_swap(root, stage, token):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("forced Redis swap failure")
        return original_swap(root, stage, token)

    monkeypatch.setattr(module, "_swap_root", fail_second_swap)
    with pytest.raises(OSError, match="forced Redis"):
        module.restore_backup(
            archive,
            key_file=key,
            data_root=target_data,
            redis_root=target_redis,
            confirm=module.CONFIRM_RESTORE,
        )
    conn = connect(str(target_data / "iic.db"))
    assert conn.execute("SELECT ticker FROM watchlist").fetchone()[0] == "ORIGINAL"
    conn.close()
    assert not list(target_data.glob(".iic-restore-*"))


@pytest.mark.unit
def test_symlink_and_missing_redis_aof_are_rejected(tmp_path):
    from tradingagents.backup.archive import BackupError, create_backup

    data, redis, output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")
    (data / "unsafe-link").symlink_to("/etc/passwd")
    with pytest.raises(BackupError, match="symbolic link"):
        create_backup(
            data_root=data, redis_root=redis, output_root=output, key_file=key
        )
    (data / "unsafe-link").unlink()
    (redis / "appendonlydir" / "appendonly.aof.manifest").unlink()
    with pytest.raises(BackupError, match="AOF manifest is missing"):
        create_backup(
            data_root=data, redis_root=redis, output_root=output, key_file=key
        )


@pytest.mark.unit
def test_status_rejects_stale_latest_backup(tmp_path):
    from tradingagents.backup.archive import BackupError, backup_status, create_backup

    data, redis, output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")
    created = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    create_backup(
        data_root=data,
        redis_root=redis,
        output_root=output,
        key_file=key,
        now=created,
    )
    with pytest.raises(BackupError, match="stale"):
        backup_status(output, now=created + timedelta(minutes=71))


@pytest.mark.unit
def test_retention_failure_does_not_unpublish_verified_latest(tmp_path, monkeypatch):
    import tradingagents.backup.archive as module

    data, redis, output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")

    def fail_retention(*_args, **_kwargs):
        raise module.BackupError("forced retention failure")

    monkeypatch.setattr(module, "prune_backups", fail_retention)
    with pytest.raises(module.BackupError, match="forced retention"):
        module.create_backup(
            data_root=data,
            redis_root=redis,
            output_root=output,
            key_file=key,
        )
    latest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
    assert (output / latest["archive"]).is_file()
    assert Path(f"{output / latest['archive']}.sha256").is_file()


@pytest.mark.unit
def test_backup_output_must_not_overlap_a_source_volume(tmp_path):
    from tradingagents.backup.archive import BackupError, create_backup

    data, redis, _output = _source_roots(tmp_path)
    key = _key(tmp_path / "backup.key")
    nested_output = data / "backups"
    with pytest.raises(BackupError, match="outside both source volumes"):
        create_backup(
            data_root=data,
            redis_root=redis,
            output_root=nested_output,
            key_file=key,
        )
    assert not nested_output.exists()


@pytest.mark.unit
def test_retention_keeps_recent_daily_weekly_and_newest(tmp_path):
    from tradingagents.backup.archive import prune_backups

    output = tmp_path / "backups"
    output.mkdir()
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    stamps = [
        now - timedelta(hours=1),
        now - timedelta(hours=49),
        now - timedelta(hours=50),
        now - timedelta(days=10),
        now - timedelta(days=10, hours=1),
        now - timedelta(days=30),
        now - timedelta(days=31),
        now - timedelta(weeks=9),
    ]
    for index, stamp in enumerate(stamps):
        name = f"iic-forge-{stamp:%Y%m%dT%H%M%SZ}-{index:012d}-test.iicbak"
        archive = output / name
        archive.write_bytes(b"encrypted")
        Path(f"{archive}.sha256").write_text("sidecar", encoding="ascii")
    removed = prune_backups(output, now=now)
    assert len(removed) == 4
    assert any((now - timedelta(weeks=9)).strftime("%Y%m%d") in name for name in removed)
    assert len(list(output.glob("*.iicbak"))) == 4
