from __future__ import annotations

import hashlib
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.persistence import db


def _migration_rows(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT version, name, checksum, applied_ts, app_version "
                "FROM schema_migrations ORDER BY version"
            )
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_sql_splitter_handles_multiple_statements_and_embedded_semicolons():
    statements = db._split_sql_statements(
        "CREATE TABLE probe (value TEXT); "
        "INSERT INTO probe (value) VALUES ('inside;value');"
    )

    assert len(statements) == 2


@pytest.mark.unit
def test_fresh_database_records_immutable_baseline(tmp_path):
    path = tmp_path / "iic.db"
    conn = db.connect(str(path))
    conn.close()

    migrations = db._load_migrations()
    rows = _migration_rows(path)

    assert len(migrations) == 1
    assert len(rows) == 1
    assert rows[0]["version"] == migrations[0].version == 1
    assert rows[0]["name"] == migrations[0].name == "baseline"
    assert rows[0]["checksum"] == migrations[0].checksum
    assert rows[0]["applied_ts"]
    assert rows[0]["app_version"]


@pytest.mark.unit
def test_reconnect_verifies_without_creating_another_backup(tmp_path):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()
    db.connect(str(path)).close()

    assert not (tmp_path / "migration-backups").exists()
    assert len(_migration_rows(path)) == 1


@pytest.mark.unit
def test_nonempty_unversioned_database_is_refused_without_mutation(tmp_path):
    path = tmp_path / "iic.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unknown_state (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO unknown_state (id) VALUES (7)")
    conn.commit()
    conn.close()

    with pytest.raises(db.MigrationPreflightError, match="clean database"):
        db.connect(str(path))

    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT id FROM unknown_state").fetchone()[0] == 7
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        check.close()
    assert not (tmp_path / "migration-backups").exists()


@pytest.mark.unit
def test_empty_migration_history_is_refused(tmp_path):
    path = tmp_path / "iic.db"
    conn = sqlite3.connect(path)
    conn.execute(db._MIGRATION_TABLE_SQL)
    conn.commit()
    conn.close()

    with pytest.raises(db.MigrationError, match="contains no applied"):
        db.connect(str(path))


@pytest.mark.unit
def test_concurrent_fresh_bootstrap_is_serialized(tmp_path):
    path = tmp_path / "iic.db"

    def _bootstrap() -> bool:
        conn = db.connect(str(path))
        try:
            return conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: _bootstrap(), range(8)))

    assert results == [True] * 8
    assert len(_migration_rows(path)) == 1
    assert not (tmp_path / "migration-backups").exists()


@pytest.mark.unit
def test_applied_migration_checksum_tampering_is_refused(tmp_path):
    path = tmp_path / "iic.db"
    conn = db.connect(str(path))
    conn.execute(
        "UPDATE schema_migrations SET checksum=? WHERE version=1",
        ("0" * 64,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(db.MigrationError, match="immutable"):
        db.connect(str(path))


@pytest.mark.unit
def test_database_newer_than_application_is_refused(tmp_path):
    path = tmp_path / "iic.db"
    conn = db.connect(str(path))
    conn.execute(
        "INSERT INTO schema_migrations "
        "(version, name, checksum, applied_ts, app_version) "
        "VALUES (2, 'future', ?, '2026-08-08T00:00:00Z', 'future')",
        ("f" * 64,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(db.MigrationError, match="newer than this application"):
        db.connect(str(path))


@pytest.mark.unit
def test_applied_migration_history_gap_is_refused(tmp_path):
    path = tmp_path / "iic.db"
    conn = db.connect(str(path))
    conn.execute(
        "INSERT INTO schema_migrations "
        "(version, name, checksum, applied_ts, app_version) "
        "VALUES (3, 'gap', ?, '2026-08-08T00:00:00Z', 'test')",
        ("f" * 64,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(db.MigrationError, match="history has gaps"):
        db.connect(str(path))


@pytest.mark.unit
def test_versioned_database_with_schema_drift_is_refused(tmp_path):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()
    drifted = sqlite3.connect(path)
    drifted.execute("DROP INDEX idx_runs_persona")
    drifted.commit()
    drifted.close()

    with pytest.raises(db.MigrationPreflightError, match="missing schema objects"):
        db.connect(str(path))


@pytest.mark.unit
def test_successful_pending_migration_is_backed_up_and_recorded(tmp_path, monkeypatch):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()

    migrations = db._load_migrations()
    pending = db.Migration.from_sql(
        2,
        "upgrade_probe",
        """
CREATE TABLE migration_probe (
    id INTEGER PRIMARY KEY,
    note TEXT NOT NULL
);
CREATE TRIGGER migration_probe_after_insert
AFTER INSERT ON migration_probe
BEGIN
    UPDATE migration_probe SET note = 'seen;ok' WHERE id = NEW.id;
END;
""",
    )
    monkeypatch.setattr(db, "_load_migrations", lambda: migrations + (pending,))

    upgraded = db.connect(str(path))
    upgraded.execute("INSERT INTO migration_probe (id, note) VALUES (1, 'new')")
    upgraded.commit()
    assert upgraded.execute(
        "SELECT note FROM migration_probe WHERE id=1"
    ).fetchone()[0] == "seen;ok"
    upgraded.close()

    assert [row["version"] for row in _migration_rows(path)] == [1, 2]
    backups = sorted((tmp_path / "migration-backups").glob("*.db"))
    assert len(backups) == 1
    assert "pre-v0002" in backups[0].name
    checksum_path = Path(f"{backups[0]}.sha256")
    assert checksum_path.is_file()
    assert stat.S_IMODE((tmp_path / "migration-backups").stat().st_mode) == 0o700
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(checksum_path.stat().st_mode) == 0o600

    previous = sqlite3.connect(backups[0])
    try:
        assert previous.execute(
            "SELECT 1 FROM sqlite_master WHERE name='migration_probe'"
        ).fetchone() is None
        assert previous.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC"
        ).fetchone()[0] == 1
    finally:
        previous.close()


@pytest.mark.unit
def test_concurrent_pending_migration_runs_once(tmp_path, monkeypatch):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()
    migrations = db._load_migrations()
    pending = db.Migration.from_sql(
        2,
        "concurrent_probe",
        "CREATE TABLE concurrent_probe (id INTEGER PRIMARY KEY);\n",
    )
    monkeypatch.setattr(db, "_load_migrations", lambda: migrations + (pending,))

    def _upgrade() -> int:
        conn = db.connect(str(path))
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version=2"
            ).fetchone()[0]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _index: _upgrade(), range(4)))

    assert results == [1] * 4
    assert [row["version"] for row in _migration_rows(path)] == [1, 2]
    assert len(list((tmp_path / "migration-backups").glob("*.db"))) == 1


@pytest.mark.unit
def test_forced_fresh_bootstrap_failure_rolls_back_every_schema_object(
    tmp_path, monkeypatch
):
    path = tmp_path / "iic.db"
    migrations = db._load_migrations()
    forced = db.Migration.from_sql(
        2,
        "forced_fresh_failure",
        """
CREATE TABLE fresh_probe (id INTEGER PRIMARY KEY);
INSERT INTO table_that_does_not_exist (id) VALUES (1);
""",
    )
    monkeypatch.setattr(db, "_load_migrations", lambda: migrations + (forced,))

    with pytest.raises(db.MigrationError, match="rolled back"):
        db.connect(str(path))

    check = sqlite3.connect(path)
    try:
        objects = list(
            check.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        assert objects == []
    finally:
        check.close()
    assert not (tmp_path / "migration-backups").exists()

    monkeypatch.setattr(db, "_load_migrations", lambda: migrations)
    db.connect(str(path)).close()
    assert [row["version"] for row in _migration_rows(path)] == [1]


@pytest.mark.unit
def test_forced_migration_failure_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()

    migrations = db._load_migrations()
    forced = db.Migration.from_sql(
        2,
        "forced_failure",
        """
CREATE TABLE migration_probe (id INTEGER PRIMARY KEY);
INSERT INTO table_that_does_not_exist (id) VALUES (1);
""",
    )
    monkeypatch.setattr(db, "_load_migrations", lambda: migrations + (forced,))

    with pytest.raises(db.MigrationError, match="rolled back"):
        db.connect(str(path))

    check = sqlite3.connect(path)
    try:
        versions = [
            row[0]
            for row in check.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1]
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE name='migration_probe'"
        ).fetchone() is None
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()

    backups = sorted((tmp_path / "migration-backups").glob("*.db"))
    assert len(backups) == 1
    assert "pre-v0002" in backups[0].name
    assert Path(f"{backups[0]}.sha256").is_file()


@pytest.mark.unit
def test_pending_migration_refuses_foreign_key_corruption(tmp_path, monkeypatch):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()
    corrupt = sqlite3.connect(path)
    corrupt.execute("PRAGMA foreign_keys=OFF")
    corrupt.execute(
        "INSERT INTO costs (run_id, provider, model) "
        "VALUES ('missing-run', 'test', 'test')"
    )
    corrupt.commit()
    corrupt.close()

    migrations = db._load_migrations()
    pending = db.Migration.from_sql(2, "safe_probe", "CREATE TABLE safe_probe (id);\n")
    monkeypatch.setattr(db, "_load_migrations", lambda: migrations + (pending,))

    with pytest.raises(db.MigrationPreflightError, match="foreign_key_check"):
        db.connect(str(path))

    assert not (tmp_path / "migration-backups").exists()


@pytest.mark.unit
def test_pending_migration_requires_backup_capacity(tmp_path, monkeypatch):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()
    migrations = db._load_migrations()
    pending = db.Migration.from_sql(2, "safe_probe", "CREATE TABLE safe_probe (id);\n")
    monkeypatch.setattr(db, "_load_migrations", lambda: migrations + (pending,))
    monkeypatch.setattr(db.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

    with pytest.raises(db.MigrationPreflightError, match="insufficient free space"):
        db.connect(str(path))

    assert [row["version"] for row in _migration_rows(path)] == [1]
    assert not list((tmp_path / "migration-backups").glob("*.db"))


@pytest.mark.unit
def test_verified_backup_restore_recovers_prior_state(tmp_path):
    path = tmp_path / "iic.db"
    conn = db.connect(str(path))
    conn.execute(
        "INSERT INTO runs "
        "(run_id, ticker, started_ts, status, artifact_dir) "
        "VALUES ('restore-run', 'AAPL', '2026-08-08T00:00:00Z', 'complete', 'r')"
    )
    conn.commit()
    conn.close()

    backup = db.create_verified_backup(path, reason="restore-test")
    checksum = Path(f"{backup}.sha256").read_text(encoding="ascii").split()[0]
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == checksum

    changed = db.connect(str(path))
    changed.execute("UPDATE runs SET ticker='MSFT' WHERE run_id='restore-run'")
    changed.commit()
    changed.close()

    rollback_backup = db.restore_database(backup, path)
    assert rollback_backup is not None and rollback_backup.is_file()

    restored = db.connect(str(path))
    try:
        assert restored.execute(
            "SELECT ticker FROM runs WHERE run_id='restore-run'"
        ).fetchone()[0] == "AAPL"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(restored.execute("PRAGMA foreign_key_check")) == []
    finally:
        restored.close()


@pytest.mark.unit
def test_restore_rejects_tampered_backup(tmp_path):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()
    backup = db.create_verified_backup(path, reason="tamper-test")
    with backup.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(db.MigrationRestoreError, match="checksum mismatch"):
        db.restore_database(backup, path)


@pytest.mark.unit
def test_failed_backup_verification_removes_partial_artifacts(tmp_path, monkeypatch):
    path = tmp_path / "iic.db"
    db.connect(str(path)).close()

    def _fail_verification(_path, *, label):
        raise db.MigrationPreflightError(f"{label} verification failed")

    monkeypatch.setattr(db, "_verify_database_file", _fail_verification)
    with pytest.raises(db.MigrationPreflightError, match="verification failed"):
        db.create_verified_backup(path, reason="forced-verification-failure")

    backup_dir = tmp_path / "migration-backups"
    assert backup_dir.is_dir()
    assert list(backup_dir.iterdir()) == []
