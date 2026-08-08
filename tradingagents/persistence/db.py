"""SQLite connections, immutable migrations, and verified local recovery points."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from typing import Literal, Set

import sqlite_vec  # type: ignore[import-untyped]


class MigrationError(RuntimeError):
    """Base error for schema migration and recovery failures."""


class MigrationPreflightError(MigrationError):
    """The database is unsafe to migrate or back up."""


class MigrationRestoreError(MigrationError):
    """A verified database backup could not be restored safely."""


@dataclass(frozen=True)
class Migration:
    """One immutable, ordered, packaged SQL migration."""

    version: int
    name: str
    sql: str
    checksum: str

    @classmethod
    def from_sql(cls, version: int, name: str, sql: str) -> "Migration":
        return cls(
            version=version,
            name=name,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )


_APPLICATION_TABLES: Set[str] = {
    "runs",
    "costs",
    "briefs",
    "brief_actions",
    "suppression",
    "memories",
    "outcome_log",
    "backtests",
    "backtest_runs",
    "events",
    "event_ticker",
    "watchlist",
    "queue_jobs",
    "deliveries",
    "ingest_cursor",
    "tickers",
    "event_fingerprints",
    "event_embeddings",
    "alert_evaluations",
    "analysis_packs",
    "shadow_eval",
    "ops_counters",
}

_EXPECTED_TABLES: Set[str] = _APPLICATION_TABLES | {"schema_migrations"}

_MIGRATION_TABLE_SQL = """
CREATE TABLE schema_migrations (
    version      INTEGER PRIMARY KEY CHECK (version > 0),
    name         TEXT NOT NULL UNIQUE,
    checksum     TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_ts   TEXT NOT NULL,
    app_version  TEXT NOT NULL
)
"""

_MIGRATION_FILE_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
_BACKUP_REASON_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_MIN_BACKUP_HEADROOM_BYTES = 10 * 1024 * 1024


def _split_sql_statements(script: str) -> list[str]:
    """Split migration SQL using SQLite's own completeness parser."""
    out: list[str] = []
    buf: list[str] = []
    for character in script:
        buf.append(character)
        if character != ";":
            continue
        candidate = "".join(buf)
        if sqlite3.complete_statement(candidate):
            out.append(candidate)
            buf = []
    remainder = "".join(buf)
    if any(
        line.strip() and not line.lstrip().startswith("--")
        for line in remainder.splitlines()
    ):
        raise MigrationError("migration SQL must terminate every statement with ';'")
    return out


def schema_tables() -> Set[str]:
    """Tables expected after a successful connection and migration."""
    return set(_EXPECTED_TABLES)


def _application_version() -> str:
    try:
        return metadata.version("iic-forge")
    except metadata.PackageNotFoundError:
        return "0.2.5+source"


def _load_migrations() -> tuple[Migration, ...]:
    root = files("tradingagents.persistence.migrations")
    discovered: list[Migration] = []
    for resource in root.iterdir():
        if not resource.is_file() or not resource.name.endswith(".sql"):
            continue
        match = _MIGRATION_FILE_RE.fullmatch(resource.name)
        if match is None:
            raise MigrationError(f"invalid packaged migration filename: {resource.name}")
        sql = resource.read_text(encoding="utf-8")
        discovered.append(
            Migration.from_sql(
                int(match.group("version")),
                match.group("name"),
                sql,
            )
        )

    discovered.sort(key=lambda migration: migration.version)
    if not discovered:
        raise MigrationError("no packaged database migrations were found")
    expected = list(range(1, len(discovered) + 1))
    versions = [migration.version for migration in discovered]
    if versions != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 1: expected {expected}, got {versions}"
        )
    return tuple(discovered)


def _apply_sql(conn: sqlite3.Connection, migration: Migration) -> None:
    for statement in _split_sql_statements(migration.sql):
        if statement.strip():
            conn.execute(statement)


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def _configure_connection(conn: sqlite3.Connection, *, enable_wal: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if enable_wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _load_vec_extension(conn)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _database_state(
    conn: sqlite3.Connection,
) -> Literal["empty", "versioned", "unversioned"]:
    objects = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'view', 'trigger', 'index') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not objects:
        return "empty"
    if ("table", "schema_migrations") in objects:
        return "versioned"
    return "unversioned"


def _ensure_vec_index(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "vec_index"):
        conn.execute("CREATE VIRTUAL TABLE vec_index USING vec0(embedding float[384])")


def _column_names(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")'))


def _schema_objects(conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    return frozenset(
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'view', 'trigger', 'index') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )


@lru_cache(maxsize=8)
def _expected_schema_shape(
    migrations: tuple[Migration, ...],
) -> tuple[dict[str, tuple[str, ...]], frozenset[tuple[str, str]]]:
    template = sqlite3.connect(":memory:")
    try:
        _configure_connection(template, enable_wal=False)
        template.execute("BEGIN IMMEDIATE")
        template.execute(_MIGRATION_TABLE_SQL)
        for migration in migrations:
            _apply_sql(template, migration)
            if migration.version == 1:
                _ensure_vec_index(template)
        template.commit()
        objects = _schema_objects(template)
        columns = {
            name: _column_names(template, name)
            for kind, name in sorted(objects)
            if kind in {"table", "view"}
        }
        return columns, objects
    finally:
        template.close()


def _validate_expected_schema(
    conn: sqlite3.Connection, migrations: tuple[Migration, ...]
) -> None:
    expected_columns, expected_objects = _expected_schema_shape(migrations)
    actual_objects = _schema_objects(conn)
    missing_objects = sorted(expected_objects - actual_objects)
    unexpected_objects = sorted(actual_objects - expected_objects)
    column_mismatches: list[str] = []
    for table, expected in expected_columns.items():
        if ("table", table) not in actual_objects and ("view", table) not in actual_objects:
            continue
        actual = _column_names(conn, table)
        if actual != expected:
            column_mismatches.append(
                f"{table}: expected {list(expected)}, found {list(actual)}"
            )

    problems: list[str] = []
    if missing_objects:
        problems.append(f"missing schema objects: {missing_objects}")
    if unexpected_objects:
        problems.append(f"unexpected schema objects: {unexpected_objects}")
    if column_mismatches:
        problems.append("column mismatches: " + "; ".join(column_mismatches))
    if problems:
        raise MigrationPreflightError(
            "database schema does not match its migration history; "
            + " | ".join(problems)
        )


def _validate_database(conn: sqlite3.Connection, *, label: str) -> None:
    integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise MigrationPreflightError(
            f"{label} failed SQLite integrity_check: {integrity_rows}"
        )
    foreign_key_rows = list(conn.execute("PRAGMA foreign_key_check"))
    if foreign_key_rows:
        rendered = [tuple(row) for row in foreign_key_rows[:10]]
        raise MigrationPreflightError(
            f"{label} failed foreign_key_check: {rendered}"
        )


def _applied_rows(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    if not _table_exists(conn, "schema_migrations"):
        return {}
    return {
        int(row["version"]): row
        for row in conn.execute(
            "SELECT version, name, checksum, applied_ts, app_version "
            "FROM schema_migrations ORDER BY version"
        )
    }


def _verify_applied_migrations(
    applied: dict[int, sqlite3.Row], migrations: tuple[Migration, ...]
) -> None:
    if not applied:
        raise MigrationError(
            "schema_migrations exists but contains no applied migration records"
        )
    versions = sorted(applied)
    expected_versions = list(range(1, versions[-1] + 1))
    if versions != expected_versions:
        raise MigrationError(
            f"applied migration history has gaps: expected {expected_versions}, got {versions}"
        )
    if versions[-1] > migrations[-1].version:
        raise MigrationError(
            f"database schema version {versions[-1]} is newer than this application "
            f"(latest supported {migrations[-1].version})"
        )
    by_version = {migration.version: migration for migration in migrations}
    for version, row in applied.items():
        expected = by_version[version]
        if row["name"] != expected.name or row["checksum"] != expected.checksum:
            raise MigrationError(
                f"applied migration {version:04d} does not match packaged "
                "name/checksum; migration files are immutable"
            )


def _database_footprint(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


def _require_backup_capacity(path: Path, backup_dir: Path) -> None:
    footprint = max(_database_footprint(path), 4096)
    required = max(footprint * 2, _MIN_BACKUP_HEADROOM_BYTES)
    free = shutil.disk_usage(backup_dir.parent).free
    if free < required:
        raise MigrationPreflightError(
            f"insufficient free space for a verified migration backup: "
            f"need at least {required} bytes, found {free}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.sha256")


def _create_private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_database_file(path: Path, *, label: str) -> None:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        _configure_connection(conn, enable_wal=False)
        _validate_database(conn, label=label)
    finally:
        conn.close()


def _create_verified_backup_from_connection(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    backup_dir: Path,
    reason: str,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    _require_backup_capacity(db_path, backup_dir)
    _validate_database(conn, label="source database")

    safe_reason = _BACKUP_REASON_RE.sub("-", reason).strip("-.") or "manual"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{db_path.stem}-{stamp}-{safe_reason}.db"
    checksum_file = _checksum_path(destination)
    _create_private_file(destination)
    try:
        backup_conn = sqlite3.connect(destination)
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        os.chmod(destination, 0o600)

        _verify_database_file(destination, label="migration backup")
        digest = _sha256_file(destination)
        _write_private_text(checksum_file, f"{digest}  {destination.name}\n")
    except Exception:
        checksum_file.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return destination


def _create_locked_migration_backup(
    db_path: Path,
    *,
    backup_dir: Path,
    reason: str,
) -> Path:
    """Back up the last commit while the migration connection owns the write lock."""
    source = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        _configure_connection(source, enable_wal=False)
        return _create_verified_backup_from_connection(
            source,
            db_path,
            backup_dir=backup_dir,
            reason=reason,
        )
    finally:
        source.close()


def create_verified_backup(
    db_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
    reason: str = "manual",
) -> Path:
    """Create and verify a local SQLite recovery point without running migrations."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise MigrationPreflightError(f"database does not exist: {path}")
    destination_dir = (
        Path(backup_dir).expanduser().resolve()
        if backup_dir is not None
        else path.parent / "migration-backups"
    )
    conn = sqlite3.connect(path)
    try:
        _configure_connection(conn, enable_wal=True)
        return _create_verified_backup_from_connection(
            conn,
            path,
            backup_dir=destination_dir,
            reason=reason,
        )
    finally:
        conn.close()


def _read_expected_backup_checksum(backup_path: Path) -> str:
    checksum_file = _checksum_path(backup_path)
    if not checksum_file.is_file():
        raise MigrationRestoreError(
            f"backup checksum sidecar is missing: {checksum_file}"
        )
    parts = checksum_file.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or parts[1] != backup_path.name:
        raise MigrationRestoreError(f"invalid backup checksum sidecar: {checksum_file}")
    return parts[0]


def restore_database(
    backup_path: str | Path,
    db_path: str | Path,
    *,
    preserve_current: bool = True,
) -> Path | None:
    """Offline restore from a verified backup; returns the pre-restore backup."""
    source = Path(backup_path).expanduser().resolve()
    target = Path(db_path).expanduser().resolve()
    if not source.is_file():
        raise MigrationRestoreError(f"backup does not exist: {source}")
    expected_checksum = _read_expected_backup_checksum(source)
    actual_checksum = _sha256_file(source)
    if actual_checksum != expected_checksum:
        raise MigrationRestoreError(
            f"backup checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
        )
    _verify_database_file(source, label="restore source")

    live_sidecars = [
        candidate
        for candidate in (Path(f"{target}-wal"), Path(f"{target}-shm"))
        if candidate.exists()
    ]
    if live_sidecars:
        raise MigrationRestoreError(
            "database appears to be open or not checkpointed; stop all writers and "
            f"remove the SQLite WAL/SHM through a clean shutdown first: {live_sidecars}"
        )

    rollback_backup: Path | None = None
    if target.exists() and preserve_current:
        rollback_backup = create_verified_backup(target, reason="pre-restore")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}.tmp")
    _create_private_file(temporary)
    source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(temporary)
    try:
        _load_vec_extension(source_conn)
        _load_vec_extension(target_conn)
        source_conn.backup(target_conn)
        _validate_database(target_conn, label="restored temporary database")
    except Exception:
        target_conn.close()
        source_conn.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        target_conn.close()
        source_conn.close()

    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    _verify_database_file(target, label="restored database")
    return rollback_backup


def _record_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    conn.execute(
        "INSERT INTO schema_migrations "
        "(version, name, checksum, applied_ts, app_version) VALUES (?, ?, ?, ?, ?)",
        (
            migration.version,
            migration.name,
            migration.checksum,
            datetime.now(timezone.utc).isoformat(),
            _application_version(),
        ),
    )


def _require_runtime_schema(
    conn: sqlite3.Connection, migrations: tuple[Migration, ...]
) -> None:
    _validate_expected_schema(conn, migrations)


def _migrate(conn: sqlite3.Connection, db_path: Path) -> None:
    migrations = _load_migrations()

    initial_state = _database_state(conn)
    if initial_state == "unversioned":
        raise MigrationPreflightError(
            "non-empty unversioned database found; IIC-Forge requires a clean "
            "database path or Docker volume for the first production bootstrap"
        )
    if initial_state == "versioned":
        applied = _applied_rows(conn)
        _verify_applied_migrations(applied, migrations)
        pending = tuple(
            migration for migration in migrations if migration.version not in applied
        )
        if not pending:
            _require_runtime_schema(conn, migrations)
            return

    try:
        conn.execute("BEGIN IMMEDIATE")
        locked_state = _database_state(conn)
        if locked_state == "unversioned":
            raise MigrationPreflightError(
                "non-empty unversioned database found; IIC-Forge requires a clean "
                "database path or Docker volume for the first production bootstrap"
            )

        if locked_state == "versioned":
            applied = _applied_rows(conn)
            _verify_applied_migrations(applied, migrations)
            pending = tuple(
                migration for migration in migrations if migration.version not in applied
            )
            if pending:
                _validate_database(conn, label="pre-migration database")
                _create_locked_migration_backup(
                    db_path,
                    backup_dir=db_path.parent / "migration-backups",
                    reason=f"pre-v{pending[0].version:04d}",
                )
            for migration in pending:
                _apply_sql(conn, migration)
                _record_migration(conn, migration)
        else:
            conn.execute(_MIGRATION_TABLE_SQL)
            for migration in migrations:
                _apply_sql(conn, migration)
                if migration.version == 1:
                    _ensure_vec_index(conn)
                _record_migration(conn, migration)

        _require_runtime_schema(conn, migrations)
        _validate_database(conn, label="migrated database")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"database migration failed and was rolled back: {exc}") from exc


def connect(db_path: str) -> sqlite3.Connection:
    """Open an IIC database and apply or verify immutable migrations."""
    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        # WAL is enabled only after state validation so opening an unversioned
        # database cannot silently alter its persistent journal mode.
        _configure_connection(conn, enable_wal=False)
        _migrate(conn, path)
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        conn.close()
        raise
    return conn
