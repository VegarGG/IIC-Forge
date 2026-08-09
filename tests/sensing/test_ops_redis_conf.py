import pytest
from pathlib import Path


@pytest.mark.unit
def test_redis_conf_has_required_settings():
    text = Path("ops/redis/redis.conf").read_text()
    assert "appendonly yes" in text
    assert "appendfsync everysec" in text
    assert "maxmemory-policy noeviction" in text
    assert "maxmemory 256mb" in text
    # RDB snapshots explicitly disabled — AOF is the source of durability.
    assert "save \"\"" in text


@pytest.mark.unit
def test_backup_script_is_executable_and_uses_compose_backup_service():
    import stat
    path = Path("ops/backup.sh")
    text = path.read_text()
    assert "docker compose stop" in text
    assert "backup-create" in text
    assert "forge backup status" in text
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR
