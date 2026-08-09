"""Preview-first retention for bounded local operational data."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONFIRM_RETENTION = "APPLY IIC-FORGE RETENTION"


def _safe_candidate(path_value: str | None, *, root: Path) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if path.is_symlink() or not resolved.is_file():
        return None
    return resolved


def _old_direct_files(root: Path, *, cutoff: datetime, suffix: str) -> list[Path]:
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix != suffix:
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff:
            candidates.append(path.resolve())
    return sorted(candidates)


def retention_candidates(
    conn: sqlite3.Connection,
    *,
    data_dir: str | Path,
    quarantine_days: int = 90,
    event_days: int = 730,
    log_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return IDs and safe paths; no deletion occurs in this function."""
    if min(quarantine_days, event_days, log_days) < 1:
        raise ValueError("retention windows must be positive")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = Path(data_dir).expanduser().resolve()
    event_root = (root / "events").resolve()
    quarantine_cutoff = (current - timedelta(days=quarantine_days)).isoformat()
    event_cutoff = (current - timedelta(days=event_days)).isoformat()

    quarantine_rows = list(
        conn.execute(
            "SELECT quarantine_id, raw_path FROM ingest_quarantine "
            "WHERE datetime(observed_ts) < datetime(?) ORDER BY quarantine_id",
            (quarantine_cutoff,),
        )
    )
    event_rows = list(
        conn.execute(
            "SELECT e.event_id, e.raw_path FROM events e "
            "WHERE datetime(e.ingested_ts) < datetime(?) "
            "AND e.status IN ('discarded', 'duplicate') "
            "AND NOT EXISTS (SELECT 1 FROM queue_jobs q WHERE q.trigger_event_id=e.event_id) "
            "AND NOT EXISTS (SELECT 1 FROM briefs b WHERE b.trigger_event_id=e.event_id) "
            "AND NOT EXISTS (SELECT 1 FROM analysis_packs a WHERE a.event_id=e.event_id) "
            "ORDER BY e.event_id",
            (event_cutoff,),
        )
    )
    paths = {
        candidate
        for row in (*quarantine_rows, *event_rows)
        if (candidate := _safe_candidate(row["raw_path"], root=event_root))
        is not None
    }
    staging = _old_direct_files(
        event_root / "staging",
        cutoff=current - timedelta(days=quarantine_days),
        suffix=".json",
    )
    logs = _old_direct_files(
        root / "logs", cutoff=current - timedelta(days=log_days), suffix=".log"
    )
    all_paths = sorted(paths | set(staging) | set(logs))
    return {
        "generated_ts": current.isoformat(),
        "data_root": str(root),
        "policy": {
            "quarantine_days": quarantine_days,
            "event_days": event_days,
            "log_days": log_days,
        },
        "quarantine_ids": [str(row["quarantine_id"]) for row in quarantine_rows],
        "event_ids": [str(row["event_id"]) for row in event_rows],
        "files": [str(path) for path in all_paths],
        "file_bytes": sum(path.stat().st_size for path in all_paths),
    }


def apply_retention(
    conn: sqlite3.Connection,
    *,
    candidates: dict[str, Any],
    operator_note: str,
    confirm: str,
) -> dict[str, int]:
    """Apply an already reviewed candidate set with exact confirmation."""
    if confirm != CONFIRM_RETENTION:
        raise ValueError(f"confirmation must be exactly {CONFIRM_RETENTION!r}")
    if not operator_note.strip():
        raise ValueError("operator note must not be empty")
    if not candidates.get("data_root"):
        raise ValueError("retention candidates are missing the bounded data root")
    root = Path(str(candidates["data_root"])).expanduser().resolve(strict=True)
    files_removed = 0
    for value in candidates.get("files") or []:
        path = Path(str(value))
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if path.is_symlink() or not resolved.is_file():
            continue
        resolved.unlink()
        files_removed += 1

    quarantine_ids = tuple(str(value) for value in candidates.get("quarantine_ids") or ())
    event_ids = tuple(str(value) for value in candidates.get("event_ids") or ())
    with conn:
        quarantine_removed = 0
        for identifier in quarantine_ids:
            quarantine_removed += conn.execute(
                "DELETE FROM ingest_quarantine WHERE quarantine_id=?", (identifier,)
            ).rowcount
        events_removed = 0
        for identifier in event_ids:
            events_removed += conn.execute(
                "DELETE FROM events WHERE event_id=? AND status IN ('discarded','duplicate')",
                (identifier,),
            ).rowcount
        conn.execute(
            "INSERT INTO operator_actions (action_type, target_type, target_id, "
            "requested_ts, operator_note, result, metadata) VALUES "
            "('retention', 'local_data', 'default', ?, ?, 'applied', ?)",
            (
                str(candidates["generated_ts"]),
                operator_note[:2000],
                json.dumps(
                    {
                        "files_removed": files_removed,
                        "quarantine_removed": quarantine_removed,
                        "events_removed": events_removed,
                    },
                    sort_keys=True,
                ),
            ),
        )
    return {
        "files_removed": files_removed,
        "quarantine_removed": quarantine_removed,
        "events_removed": events_removed,
    }
