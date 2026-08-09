from __future__ import annotations

import stat
from pathlib import Path

import pytest


@pytest.mark.unit
def test_backup_wrapper_is_generic_encrypted_and_rpo_safe():
    path = Path("ops/backup.sh")
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & stat.S_IXUSR
    assert "docker compose stop" in text
    assert "backup-create" in text
    assert "forge backup status" in text
    assert "--max-age-minutes 60" in text
    assert "flock -n" in text
    assert "ziwei-huang" not in text
    assert "sqlite3" not in text
    assert "docker cp" not in text


@pytest.mark.unit
def test_restore_wrapper_requires_explicit_confirmation_and_prebackup():
    path = Path("ops/restore.sh")
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & stat.S_IXUSR
    assert "RESTORE_IIC_FORGE_LOCAL_BACKUP" in text
    assert "forge backup verify" in text
    assert "--label pre-restore --no-prune" in text
    assert "backup-restore" in text
    assert "forge runtime health --database --redis" in text
