"""Isolated restore-drill evidence for the operator preflight."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.backup import restore_backup
from tradingagents.backup.archive import CONFIRM_RESTORE


CONFIRM_DRILL = "RUN RESTORE DRILL"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_restore_drill(
    conn: sqlite3.Connection,
    *,
    archive: str | Path,
    key_file: str | Path,
    operator_note: str,
    confirm: str,
) -> dict[str, Any]:
    """Restore into disposable roots and persist pass/fail evidence."""
    if confirm != CONFIRM_DRILL:
        raise ValueError(f"confirmation must be exactly {CONFIRM_DRILL!r}")
    if not operator_note.strip():
        raise ValueError("operator note must not be empty")
    archive_path = Path(archive).expanduser().resolve()
    started = datetime.now(timezone.utc)
    tick = time.monotonic()
    status = "failed"
    error_type: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="iic-restore-drill-") as root:
            data = Path(root) / "data"
            redis = Path(root) / "redis"
            data.mkdir(mode=0o700)
            redis.mkdir(mode=0o700)
            result = restore_backup(
                archive_path,
                key_file=key_file,
                data_root=data,
                redis_root=redis,
                confirm=CONFIRM_RESTORE,
            )
            status = "passed"
    except Exception as exc:
        error_type = type(exc).__name__
        result = {"status": "failed", "error": error_type}
    finished = datetime.now(timezone.utc)
    elapsed = max(0.0, time.monotonic() - tick)
    try:
        checksum = _sha256(archive_path)
    except OSError:
        checksum = "0" * 64
    note = operator_note.strip()[:2000]
    if error_type:
        note = f"{note}; error_type={error_type}"[:2000]
    with conn:
        conn.execute(
            "INSERT INTO recovery_drills (archive_name, archive_sha256, started_ts, "
            "finished_ts, elapsed_seconds, status, operator_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                archive_path.name,
                checksum,
                started.isoformat(),
                finished.isoformat(),
                elapsed,
                status,
                note,
            ),
        )
    return {
        "archive": archive_path.name,
        "archive_sha256": checksum,
        "status": status,
        "elapsed_seconds": elapsed,
        "restore": result,
    }
