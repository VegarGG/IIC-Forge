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
def test_backup_script_is_executable_and_handles_both_stores():
    import stat
    path = Path("ops/backup.sh")
    text = path.read_text()
    assert "s.backup(d)" in text                 # SQLite online backup API
    assert "redis-cli SAVE" in text              # synchronous RDB snapshot
    assert "dump.rdb" in text                    # pulled from the compose redis volume
    assert "_iic_redis_data" in text
    assert "_iic_data" in text
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR
