"""Encrypted, authenticated local snapshots of the data and Redis volumes.

The host wrapper stops every Compose service before invoking this module.  A
stopped stack gives one coherent recovery point across SQLite, raw artifacts,
Telegram session state, and the Redis multi-part AOF.  Archives are gzip tar
streams encrypted with AES-256-GCM; plaintext is never written to the backup
filesystem.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC = b"IICBKP01"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024
FORMAT_VERSION = 1
CONFIRM_RESTORE = "RESTORE_IIC_FORGE_LOCAL_BACKUP"
_MIN_FREE_BYTES = 64 * 1024 * 1024


class BackupError(RuntimeError):
    """A backup cannot be created, authenticated, verified, or restored."""


@dataclass(frozen=True)
class _SourceEntry:
    archive_path: str
    source_path: Path
    kind: str
    size: int
    sha256: str | None
    mode: int
    uid: int
    gid: int
    modified_ns: int

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "path": self.archive_path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "modified_ns": self.modified_ns,
        }


def _application_version() -> str:
    try:
        return version("iic-forge")
    except PackageNotFoundError:
        return "source-checkout"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def load_encryption_key(key_file: str | Path) -> bytes:
    """Load one base64-encoded, 32-byte AES key without accepting passwords."""
    path = Path(key_file).expanduser().resolve()
    if not path.is_file():
        raise BackupError(f"backup encryption key does not exist: {path}")
    try:
        encoded = b"".join(path.read_bytes().split())
        key = base64.b64decode(encoded, validate=True)
    except (OSError, binascii.Error) as exc:
        raise BackupError("backup encryption key must be valid base64") from exc
    if len(key) != 32:
        raise BackupError("backup encryption key must decode to exactly 32 bytes")
    return key


def _safe_root(value: str | Path, *, label: str, writable: bool) -> Path:
    root = Path(value).expanduser().resolve()
    if root == Path(root.anchor):
        raise BackupError(f"{label} must not be a filesystem root")
    if not root.is_dir():
        raise BackupError(f"{label} is not a directory: {root}")
    if writable and not os.access(root, os.W_OK | os.X_OK):
        raise BackupError(f"{label} is not writable: {root}")
    return root


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _validate_sqlite(data_root: Path) -> dict[str, Any]:
    database = data_root / "iic.db"
    if not database.is_file():
        raise BackupError(f"IIC database is missing: {database}")
    conn = sqlite3.connect(f"{database.as_uri()}?mode=ro", timeout=30, uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = list(conn.execute("PRAGMA foreign_key_check"))
        migrations = [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite verification failed: {type(exc).__name__}") from exc
    finally:
        conn.close()
    if integrity != ["ok"] or foreign_keys or not migrations:
        raise BackupError(
            "SQLite verification failed: "
            f"integrity={integrity!r}, foreign_key_violations={len(foreign_keys)}, "
            f"migrations={migrations!r}"
        )
    return {
        "path": "data/iic.db",
        "integrity": "ok",
        "foreign_key_violations": 0,
        "migrations": migrations,
    }


def _validate_redis_snapshot(redis_root: Path) -> dict[str, Any]:
    appendonly = redis_root / "appendonlydir"
    manifest = appendonly / "appendonly.aof.manifest"
    if not manifest.is_file():
        raise BackupError(
            "Redis AOF manifest is missing; stop Redis cleanly before backup and "
            f"confirm appendonly persistence under {appendonly}"
        )
    return {"manifest": "redis/appendonlydir/appendonly.aof.manifest"}


def _scan_root(root: Path, prefix: str) -> list[_SourceEntry]:
    entries: list[_SourceEntry] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        for name in [*directory_names, *file_names]:
            path = current_path / name
            info = path.lstat()
            relative = path.relative_to(root)
            archive_path = (PurePosixPath(prefix) / PurePosixPath(relative.as_posix())).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise BackupError(f"backup source contains a symbolic link: {archive_path}")
            if stat.S_ISDIR(info.st_mode):
                entries.append(
                    _SourceEntry(
                        archive_path,
                        path,
                        "directory",
                        0,
                        None,
                        stat.S_IMODE(info.st_mode),
                        info.st_uid,
                        info.st_gid,
                        info.st_mtime_ns,
                    )
                )
                continue
            if not stat.S_ISREG(info.st_mode):
                raise BackupError(
                    f"backup source contains a non-regular file: {archive_path}"
                )
            entries.append(
                _SourceEntry(
                    archive_path,
                    path,
                    "file",
                    info.st_size,
                    _sha256_file(path),
                    stat.S_IMODE(info.st_mode),
                    info.st_uid,
                    info.st_gid,
                    info.st_mtime_ns,
                )
            )
    return entries


class _EncryptingWriter(io.RawIOBase):
    def __init__(self, output: BinaryIO, key: bytes) -> None:
        super().__init__()
        self._output = output
        self._nonce = os.urandom(NONCE_BYTES)
        self._header = MAGIC + self._nonce
        self._output.write(self._header)
        self._encryptor = Cipher(
            algorithms.AES(key), modes.GCM(self._nonce)
        ).encryptor()
        self._encryptor.authenticate_additional_data(self._header)
        self._finalized = False

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        if self._finalized:
            raise ValueError("encrypted backup stream is finalized")
        payload = bytes(data)
        encrypted = self._encryptor.update(payload)
        if encrypted:
            self._output.write(encrypted)
        return len(payload)

    def finalize(self) -> None:
        if self._finalized:
            return
        remainder = self._encryptor.finalize()
        if remainder:
            self._output.write(remainder)
        self._output.write(self._encryptor.tag)
        self._finalized = True


class _DecryptingReader(io.RawIOBase):
    def __init__(self, source: BinaryIO, key: bytes) -> None:
        super().__init__()
        self._source = source
        size = os.fstat(source.fileno()).st_size
        minimum = len(MAGIC) + NONCE_BYTES + TAG_BYTES + 1
        if size < minimum:
            raise BackupError("backup is too small to contain an authenticated archive")
        header = source.read(len(MAGIC) + NONCE_BYTES)
        if not header.startswith(MAGIC) or len(header) != len(MAGIC) + NONCE_BYTES:
            raise BackupError("backup format header is invalid")
        source.seek(-TAG_BYTES, os.SEEK_END)
        tag = source.read(TAG_BYTES)
        source.seek(len(header))
        nonce = header[len(MAGIC) :]
        self._remaining = size - len(header) - TAG_BYTES
        self._decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        self._decryptor.authenticate_additional_data(header)
        self._finalized = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._finalized:
            return 0
        if self._remaining == 0:
            self._authenticate()
            return 0
        count = min(len(buffer), self._remaining, CHUNK_BYTES)
        encrypted = self._source.read(count)
        if len(encrypted) != count:
            raise BackupError("encrypted backup ended unexpectedly")
        self._remaining -= count
        plaintext = self._decryptor.update(encrypted)
        buffer[: len(plaintext)] = plaintext
        if self._remaining == 0:
            self._authenticate()
        return len(plaintext)

    def _authenticate(self) -> None:
        if self._finalized:
            return
        try:
            remainder = self._decryptor.finalize()
        except InvalidTag as exc:
            raise BackupError("backup authentication failed") from exc
        if remainder:
            raise BackupError("backup decryptor returned an unexpected trailer")
        self._finalized = True


def _tar_info(entry: _SourceEntry) -> tarfile.TarInfo:
    info = tarfile.TarInfo(entry.archive_path)
    info.mode = entry.mode
    info.uid = entry.uid
    info.gid = entry.gid
    info.mtime = entry.modified_ns // 1_000_000_000
    if entry.kind == "directory":
        info.type = tarfile.DIRTYPE
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.size = entry.size
    return info


def _add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o600
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    archive.addfile(info, io.BytesIO(content))


def _build_manifest(
    entries: Iterable[_SourceEntry],
    *,
    created: datetime,
    sqlite_status: dict[str, Any],
    redis_status: dict[str, Any],
) -> dict[str, Any]:
    files = [entry.manifest_dict() for entry in entries]
    return {
        "format": "iic-forge-local-backup",
        "format_version": FORMAT_VERSION,
        "created_utc": created.astimezone(timezone.utc).isoformat(),
        "application_version": _application_version(),
        "encryption": "AES-256-GCM",
        "consistency": "offline-compose-stop",
        "sqlite": sqlite_status,
        "redis": redis_status,
        "files": files,
        "file_count": len(files),
        "uncompressed_bytes": sum(item["size"] for item in files),
    }


def _require_output_capacity(output_root: Path, required_bytes: int) -> None:
    required = max(int(required_bytes * 1.05), _MIN_FREE_BYTES)
    available = shutil.disk_usage(output_root).free
    if available < required:
        raise BackupError(
            f"insufficient local backup capacity: need at least {required} bytes, "
            f"found {available}"
        )


def _checksum_sidecar(archive: Path) -> Path:
    return archive.with_name(f"{archive.name}.sha256")


def _write_cipher_checksum(archive: Path) -> str:
    digest = _sha256_file(archive)
    _write_private_atomic(
        _checksum_sidecar(archive), f"{digest}  {archive.name}\n".encode("ascii")
    )
    return digest


def _verify_cipher_checksum(archive: Path) -> str:
    sidecar = _checksum_sidecar(archive)
    if not sidecar.is_file():
        raise BackupError(f"backup checksum sidecar is missing: {sidecar}")
    parts = sidecar.read_text(encoding="ascii").strip().split()
    if len(parts) != 2 or parts[1] != archive.name or len(parts[0]) != 64:
        raise BackupError(f"backup checksum sidecar is invalid: {sidecar}")
    actual = _sha256_file(archive)
    if actual != parts[0]:
        raise BackupError("encrypted backup checksum does not match its sidecar")
    return actual


def _safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] not in {"data", "redis"}
    ):
        raise BackupError(f"unsafe backup member path: {name!r}")
    return path


def _read_manifest(archive: tarfile.TarFile) -> dict[str, Any]:
    member = archive.next()
    if member is None or member.name != "manifest.json" or not member.isfile():
        raise BackupError("encrypted archive does not start with manifest.json")
    if member.size > 16 * 1024 * 1024:
        raise BackupError("backup manifest is unreasonably large")
    stream = archive.extractfile(member)
    if stream is None:
        raise BackupError("backup manifest cannot be read")
    try:
        manifest = json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is invalid JSON") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "iic-forge-local-backup"
        or manifest.get("format_version") != FORMAT_VERSION
        or not isinstance(manifest.get("files"), list)
    ):
        raise BackupError("backup manifest contract is unsupported")
    return manifest


def _manifest_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BackupError("backup manifest contains an invalid file entry")
        name = item["path"]
        _safe_member_name(name)
        if name in expected or item.get("kind") not in {"file", "directory"}:
            raise BackupError("backup manifest contains duplicate or invalid entries")
        if item.get("kind") == "file":
            if (
                not isinstance(item.get("size"), int)
                or item["size"] < 0
                or not isinstance(item.get("sha256"), str)
                or len(item["sha256"]) != 64
            ):
                raise BackupError(f"backup manifest file metadata is invalid: {name}")
        expected[name] = item
    if manifest.get("file_count") != len(expected):
        raise BackupError("backup manifest file count does not match its entries")
    required = {
        str((manifest.get("sqlite") or {}).get("path") or ""),
        str((manifest.get("redis") or {}).get("manifest") or ""),
    }
    if not required <= set(expected):
        raise BackupError("backup manifest omits required SQLite or Redis state")
    return expected


def _open_decrypted_tar(archive_path: Path, key: bytes):
    source = archive_path.open("rb")
    try:
        raw = _DecryptingReader(source, key)
        buffered = io.BufferedReader(raw, buffer_size=CHUNK_BYTES)
        archive = tarfile.open(fileobj=buffered, mode="r|gz")
    except Exception:
        source.close()
        raise
    return source, raw, buffered, archive


def _finish_decrypted_tar(
    source: BinaryIO,
    raw: _DecryptingReader,
    buffered: io.BufferedReader,
    archive: tarfile.TarFile,
) -> None:
    try:
        archive.close()
        while buffered.read(CHUNK_BYTES):
            pass
        raw._authenticate()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError("backup archive stream is corrupt") from exc
    finally:
        buffered.close()
        source.close()


def verify_backup(
    archive_path: str | Path,
    *,
    key_file: str | Path,
    scratch_root: str | Path | None = None,
) -> dict[str, Any]:
    """Authenticate every byte, verify every member hash, then check SQLite."""
    path = Path(archive_path).expanduser().resolve()
    if not path.is_file():
        raise BackupError(f"backup archive does not exist: {path}")
    cipher_sha256 = _verify_cipher_checksum(path)
    key = load_encryption_key(key_file)
    scratch_parent = Path(scratch_root).resolve() if scratch_root else None
    with tempfile.TemporaryDirectory(
        prefix="iic-forge-backup-verify-", dir=scratch_parent
    ) as temporary:
        sqlite_files = {
            "data/iic.db": Path(temporary) / "iic.db",
            "data/iic.db-wal": Path(temporary) / "iic.db-wal",
            "data/iic.db-shm": Path(temporary) / "iic.db-shm",
        }
        source, raw, buffered, archive = _open_decrypted_tar(path, key)
        manifest: dict[str, Any] | None = None
        seen: set[str] = set()
        try:
            manifest = _read_manifest(archive)
            expected = _manifest_entries(manifest)
            while (member := archive.next()) is not None:
                name = member.name
                _safe_member_name(name)
                item = expected.get(name)
                if item is None or name in seen:
                    raise BackupError(f"unexpected or duplicate backup member: {name}")
                seen.add(name)
                expected_directory = item["kind"] == "directory"
                if member.isdir() != expected_directory:
                    raise BackupError(f"backup member type does not match manifest: {name}")
                if expected_directory:
                    continue
                if not member.isfile() or member.size != item["size"]:
                    raise BackupError(f"backup member size/type is invalid: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise BackupError(f"backup member cannot be read: {name}")
                digest = hashlib.sha256()
                sqlite_output = sqlite_files.get(name)
                output = sqlite_output.open("wb") if sqlite_output else None
                try:
                    for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                        digest.update(chunk)
                        if output is not None:
                            output.write(chunk)
                finally:
                    if output is not None:
                        output.close()
                if digest.hexdigest() != item["sha256"]:
                    raise BackupError(f"backup member hash does not match: {name}")
            if seen != set(expected):
                missing = sorted(set(expected) - seen)
                raise BackupError(f"backup archive is missing members: {missing[:5]}")
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise BackupError("backup archive stream is corrupt") from exc
        finally:
            _finish_decrypted_tar(source, raw, buffered, archive)
        if manifest is None:
            raise BackupError("backup manifest was not loaded")
        sqlite_status = _validate_sqlite(Path(temporary))
        if sqlite_status["migrations"] != manifest["sqlite"].get("migrations"):
            raise BackupError("restored SQLite migration set differs from the manifest")
        return {
            "archive": str(path),
            "created_utc": manifest["created_utc"],
            "cipher_sha256": cipher_sha256,
            "file_count": manifest["file_count"],
            "uncompressed_bytes": manifest["uncompressed_bytes"],
            "sqlite_migrations": sqlite_status["migrations"],
            "status": "verified",
        }


def _parse_archive_time(path: Path) -> datetime | None:
    parts = path.name.split("-")
    if len(parts) < 5 or parts[:2] != ["iic", "forge"]:
        return None
    try:
        return datetime.strptime(parts[2], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def prune_backups(
    output_root: str | Path,
    *,
    now: datetime | None = None,
    keep_hourly_hours: int = 48,
    keep_daily_days: int = 14,
    keep_weekly_weeks: int = 8,
) -> list[str]:
    """Apply local GFS retention without touching unrecognized files."""
    if min(keep_hourly_hours, keep_daily_days, keep_weekly_weeks) < 1:
        raise BackupError("backup retention values must all be positive")
    root = _safe_root(output_root, label="backup output root", writable=True)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = [
        (stamp, path)
        for path in root.glob("iic-forge-*.iicbak")
        if (stamp := _parse_archive_time(path)) is not None
    ]
    candidates.sort(reverse=True)
    keep: set[Path] = set()
    daily_buckets: set[str] = set()
    weekly_buckets: set[str] = set()
    hourly_cutoff = current - timedelta(hours=keep_hourly_hours)
    daily_cutoff = current - timedelta(days=keep_daily_days)
    weekly_cutoff = current - timedelta(weeks=keep_weekly_weeks)
    for stamp, path in candidates:
        if stamp >= hourly_cutoff:
            keep.add(path)
        elif stamp >= daily_cutoff:
            bucket = stamp.date().isoformat()
            if bucket not in daily_buckets:
                daily_buckets.add(bucket)
                keep.add(path)
        elif stamp >= weekly_cutoff:
            iso = stamp.isocalendar()
            bucket = f"{iso.year}-W{iso.week:02d}"
            if bucket not in weekly_buckets:
                weekly_buckets.add(bucket)
                keep.add(path)
    if candidates:
        keep.add(candidates[0][1])
    removed: list[str] = []
    for _stamp, path in candidates:
        if path in keep:
            continue
        _checksum_sidecar(path).unlink(missing_ok=True)
        path.unlink()
        removed.append(path.name)
    if removed:
        _fsync_directory(root)
    return removed


def create_backup(
    *,
    data_root: str | Path,
    redis_root: str | Path,
    output_root: str | Path,
    key_file: str | Path,
    label: str = "scheduled",
    now: datetime | None = None,
    keep_hourly_hours: int = 48,
    keep_daily_days: int = 14,
    keep_weekly_weeks: int = 8,
    apply_retention: bool = True,
) -> dict[str, Any]:
    """Create, authenticate, verify, publish, and retain one local snapshot."""
    data = _safe_root(data_root, label="data source root", writable=True)
    redis = _safe_root(redis_root, label="Redis source root", writable=False)
    if _paths_overlap(data, redis):
        raise BackupError("data and Redis source roots must not overlap")
    output = Path(output_root).expanduser().resolve()
    if _paths_overlap(output, data) or _paths_overlap(output, redis):
        raise BackupError("backup output root must be outside both source volumes")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    output = _safe_root(output, label="backup output root", writable=True)
    key = load_encryption_key(key_file)
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sqlite_status = _validate_sqlite(data)
    redis_status = _validate_redis_snapshot(redis)
    entries = [*_scan_root(data, "data"), *_scan_root(redis, "redis")]
    manifest = _build_manifest(
        entries,
        created=created,
        sqlite_status=sqlite_status,
        redis_status=redis_status,
    )
    _require_output_capacity(output, manifest["uncompressed_bytes"])
    safe_label = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in label.strip().lower()
    ).strip(".-")[:48] or "manual"
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    name = f"iic-forge-{stamp}-{uuid.uuid4().hex[:12]}-{safe_label}.iicbak"
    destination = output / name
    temporary = output / f".{name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as raw_output:
            encrypted = _EncryptingWriter(raw_output, key)
            with tarfile.open(
                fileobj=cast(BinaryIO, encrypted), mode="w|gz"
            ) as tar:
                manifest_bytes = json.dumps(
                    manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                _add_bytes(tar, "manifest.json", manifest_bytes)
                for entry in entries:
                    info = _tar_info(entry)
                    if entry.kind == "directory":
                        tar.addfile(info)
                    else:
                        with entry.source_path.open("rb") as source:
                            tar.addfile(info, source)
            encrypted.finalize()
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(output)
        cipher_sha256 = _write_cipher_checksum(destination)
        verification = verify_backup(destination, key_file=key_file)
        latest = {
            "archive": destination.name,
            "created_utc": manifest["created_utc"],
            "cipher_sha256": cipher_sha256,
            "file_count": manifest["file_count"],
            "uncompressed_bytes": manifest["uncompressed_bytes"],
            "verification": verification["status"],
        }
        _write_private_atomic(
            output / "latest.json",
            (json.dumps(latest, sort_keys=True) + "\n").encode("utf-8"),
        )
        published = True
        # Publish the new verified recovery point before pruning old ones. If
        # retention fails, the caller receives an error but latest remains a
        # usable archive rather than pointing at a file cleaned up by rollback.
        removed = (
            prune_backups(
                output,
                now=created,
                keep_hourly_hours=keep_hourly_hours,
                keep_daily_days=keep_daily_days,
                keep_weekly_weeks=keep_weekly_weeks,
            )
            if apply_retention
            else []
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        if not published:
            _checksum_sidecar(destination).unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
        raise
    return {**latest, "path": str(destination), "retention_removed": removed}


def backup_status(
    output_root: str | Path,
    *,
    max_age_minutes: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check that the latest verified snapshot is present, intact, and recent."""
    if max_age_minutes < 1:
        raise BackupError("maximum backup age must be positive")
    root = _safe_root(output_root, label="backup output root", writable=False)
    latest_path = root / "latest.json"
    if not latest_path.is_file():
        raise BackupError(f"latest backup marker is missing: {latest_path}")
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        archive_name = str(latest["archive"])
        created = datetime.fromisoformat(str(latest["created_utc"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupError("latest backup marker is invalid") from exc
    if Path(archive_name).name != archive_name or not archive_name.endswith(".iicbak"):
        raise BackupError("latest backup marker contains an unsafe archive name")
    if created.tzinfo is None:
        raise BackupError("latest backup marker timestamp is not timezone-aware")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current - created.astimezone(timezone.utc)
    if age < timedelta(minutes=-5):
        raise BackupError("latest backup timestamp is unexpectedly in the future")
    if age > timedelta(minutes=max_age_minutes):
        raise BackupError(
            f"latest verified backup is stale: age={age.total_seconds() / 60:.1f} minutes"
        )
    archive = root / archive_name
    cipher_sha256 = _verify_cipher_checksum(archive)
    if cipher_sha256 != latest.get("cipher_sha256"):
        raise BackupError("latest backup marker checksum does not match the archive")
    return {
        "archive": archive_name,
        "created_utc": created.astimezone(timezone.utc).isoformat(),
        "age_minutes": max(0.0, age.total_seconds() / 60),
        "cipher_sha256": cipher_sha256,
        "status": "current",
    }


def _apply_metadata(path: Path, item: dict[str, Any]) -> None:
    os.chmod(path, int(item["mode"]) & 0o777)
    if os.geteuid() == 0:
        os.chown(path, int(item["uid"]), int(item["gid"]), follow_symlinks=False)


def _extract_to_staging(
    archive_path: Path,
    *,
    key: bytes,
    manifest: dict[str, Any],
    stage_data: Path,
    stage_redis: Path,
) -> None:
    expected = _manifest_entries(manifest)
    directories: list[tuple[Path, dict[str, Any]]] = []
    seen: set[str] = set()
    source, raw, buffered, archive = _open_decrypted_tar(archive_path, key)
    try:
        stream_manifest = _read_manifest(archive)
        if stream_manifest != manifest:
            raise BackupError("backup manifest changed between verification and restore")
        while (member := archive.next()) is not None:
            pure = _safe_member_name(member.name)
            item = expected.get(member.name)
            if item is None or member.name in seen:
                raise BackupError(f"unexpected or duplicate backup member: {member.name}")
            seen.add(member.name)
            root = stage_data if pure.parts[0] == "data" else stage_redis
            relative = PurePosixPath(*pure.parts[1:])
            destination = root.joinpath(*relative.parts)
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise BackupError(f"unsafe restore destination: {member.name}") from exc
            if item["kind"] == "directory":
                if not member.isdir():
                    raise BackupError(f"restore member type mismatch: {member.name}")
                destination.mkdir(parents=True, exist_ok=False)
                directories.append((destination, item))
                continue
            if not member.isfile() or member.size != item["size"]:
                raise BackupError(f"restore member size/type mismatch: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            input_stream = archive.extractfile(member)
            if input_stream is None:
                raise BackupError(f"restore member cannot be read: {member.name}")
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as output:
                for chunk in iter(lambda: input_stream.read(CHUNK_BYTES), b""):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != item["sha256"]:
                raise BackupError(f"restore member hash mismatch: {member.name}")
            _apply_metadata(destination, item)
        if seen != set(expected):
            raise BackupError("restore stream did not contain every manifest entry")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError("backup restore stream is corrupt") from exc
    finally:
        _finish_decrypted_tar(source, raw, buffered, archive)
    for path, item in sorted(directories, key=lambda pair: len(pair[0].parts), reverse=True):
        _apply_metadata(path, item)


def _ensure_clean_restore_root(root: Path) -> None:
    leftovers = [
        path.name
        for path in root.iterdir()
        if path.name.startswith(".iic-restore-")
    ]
    if leftovers:
        raise BackupError(
            f"restore root contains unresolved prior staging state: {sorted(leftovers)}"
        )


def _swap_root(root: Path, stage: Path, token: str) -> Path:
    rollback = root / f".iic-restore-rollback-{token}"
    rollback.mkdir(mode=0o700)
    moved_old: list[Path] = []
    moved_new: list[Path] = []
    try:
        for current in list(root.iterdir()):
            if current in {stage, rollback}:
                continue
            os.replace(current, rollback / current.name)
            moved_old.append(current)
        for current in list(stage.iterdir()):
            os.replace(current, root / current.name)
            moved_new.append(current)
        stage.rmdir()
        _fsync_directory(root)
    except Exception:
        for original in reversed(moved_new):
            replacement = root / original.name
            if replacement.exists():
                os.replace(replacement, stage / original.name)
        for original in reversed(moved_old):
            saved = rollback / original.name
            if saved.exists():
                os.replace(saved, root / original.name)
        rollback.rmdir()
        raise
    return rollback


def _rollback_swap(root: Path, rollback: Path) -> None:
    failed = root / f".iic-restore-failed-{uuid.uuid4().hex[:12]}"
    failed.mkdir(mode=0o700)
    for current in list(root.iterdir()):
        if current in {rollback, failed}:
            continue
        os.replace(current, failed / current.name)
    for saved in list(rollback.iterdir()):
        os.replace(saved, root / saved.name)
    rollback.rmdir()
    shutil.rmtree(failed)
    _fsync_directory(root)


def restore_backup(
    archive_path: str | Path,
    *,
    key_file: str | Path,
    data_root: str | Path,
    redis_root: str | Path,
    confirm: str,
) -> dict[str, Any]:
    """Offline, fail-closed restore into two stopped Compose volumes."""
    if confirm != CONFIRM_RESTORE:
        raise BackupError(f"restore requires exact confirmation {CONFIRM_RESTORE!r}")
    archive = Path(archive_path).expanduser().resolve()
    data = _safe_root(data_root, label="data restore root", writable=True)
    redis = _safe_root(redis_root, label="Redis restore root", writable=True)
    if _paths_overlap(data, redis):
        raise BackupError("data and Redis restore roots must not overlap")
    if data in archive.parents or redis in archive.parents:
        raise BackupError("backup archive must be outside both restore volumes")
    key_path = Path(key_file).expanduser().resolve()
    if data in key_path.parents or redis in key_path.parents:
        raise BackupError("backup encryption key must be outside both restore volumes")
    _ensure_clean_restore_root(data)
    _ensure_clean_restore_root(redis)
    verification = verify_backup(archive, key_file=key_file)
    key = load_encryption_key(key_file)

    source, raw, buffered, tar = _open_decrypted_tar(archive, key)
    try:
        manifest = _read_manifest(tar)
    finally:
        _finish_decrypted_tar(source, raw, buffered, tar)
    expected = _manifest_entries(manifest)
    required_by_root = {
        prefix: sum(
            int(item["size"])
            for name, item in expected.items()
            if name.startswith(f"{prefix}/") and item["kind"] == "file"
        )
        for prefix in ("data", "redis")
    }
    for root, prefix in ((data, "data"), (redis, "redis")):
        available = shutil.disk_usage(root).free
        required = max(required_by_root[prefix], _MIN_FREE_BYTES)
        if available < required:
            raise BackupError(
                f"insufficient restore capacity in {root}: need {required}, found {available}"
            )

    token = uuid.uuid4().hex[:12]
    stage_data = data / f".iic-restore-stage-{token}"
    stage_redis = redis / f".iic-restore-stage-{token}"
    stage_data.mkdir(mode=0o700)
    stage_redis.mkdir(mode=0o700)
    rollback_data: Path | None = None
    rollback_redis: Path | None = None
    try:
        _extract_to_staging(
            archive,
            key=key,
            manifest=manifest,
            stage_data=stage_data,
            stage_redis=stage_redis,
        )
        staged_sqlite = _validate_sqlite(stage_data)
        if staged_sqlite["migrations"] != manifest["sqlite"].get("migrations"):
            raise BackupError("staged SQLite migration set differs from the manifest")
        _validate_redis_snapshot(stage_redis)
        rollback_data = _swap_root(data, stage_data, token)
        try:
            rollback_redis = _swap_root(redis, stage_redis, token)
        except Exception:
            _rollback_swap(data, rollback_data)
            rollback_data = None
            raise
        _validate_sqlite(data)
        _validate_redis_snapshot(redis)
    except Exception:
        if rollback_redis is not None:
            _rollback_swap(redis, rollback_redis)
            rollback_redis = None
        if rollback_data is not None:
            _rollback_swap(data, rollback_data)
            rollback_data = None
        if stage_data.exists():
            shutil.rmtree(stage_data)
        if stage_redis.exists():
            shutil.rmtree(stage_redis)
        raise
    if rollback_data is not None:
        shutil.rmtree(rollback_data)
    if rollback_redis is not None:
        shutil.rmtree(rollback_redis)
    _fsync_directory(data)
    _fsync_directory(redis)
    return {
        "archive": str(archive),
        "created_utc": verification["created_utc"],
        "file_count": verification["file_count"],
        "sqlite_migrations": verification["sqlite_migrations"],
        "status": "restored",
    }
