#!/usr/bin/env python3
"""Build wheel/sdist twice and publish them only when byte-for-byte identical."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: _sha256(path)
        for path in sorted(directory.iterdir())
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    }


def compare_artifacts(first: Path, second: Path) -> dict[str, str]:
    left = artifact_hashes(first)
    right = artifact_hashes(second)
    if not left or set(left) != set(right):
        raise ValueError(
            f"artifact sets differ or are empty: first={sorted(left)} second={sorted(right)}"
        )
    differences = [name for name in left if left[name] != right[name]]
    if differences:
        raise ValueError("non-reproducible artifacts: " + ", ".join(differences))
    return left


def normalize_sdist(path: Path, *, source_date_epoch: int) -> None:
    """Rewrite an sdist with deterministic archive metadata.

    Setuptools currently preserves host ownership and sub-second mtimes in PAX
    headers even when ``SOURCE_DATE_EPOCH`` is set.  The source payload is left
    unchanged; only archive order, ownership, timestamps, and gzip metadata are
    normalized before the two independent builds are compared.
    """

    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".normalized", dir=path.parent, delete=False
    )
    normalized_path = Path(temporary.name)
    temporary.close()
    try:
        with tarfile.open(path, "r:gz") as source:
            members = sorted(source.getmembers(), key=lambda member: member.name)
            with normalized_path.open("wb") as destination:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=destination,
                    compresslevel=9,
                    mtime=source_date_epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                    ) as archive:
                        for original in members:
                            member = copy.copy(original)
                            member.mtime = source_date_epoch
                            member.uid = 0
                            member.gid = 0
                            member.uname = ""
                            member.gname = ""
                            member.pax_headers = {}
                            payload = source.extractfile(original) if original.isfile() else None
                            archive.addfile(member, payload)
        os.replace(normalized_path, path)
    finally:
        normalized_path.unlink(missing_ok=True)


def _git_commit_epoch() -> int:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


def _build(output: Path, *, source_date_epoch: int) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
        }
    )
    # Launch outside the source tree. An ignored ``build/`` directory from a
    # previous local run must never shadow the installed PyPA build frontend.
    with tempfile.TemporaryDirectory(prefix="iic-build-launch-") as launch_root:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(output),
                str(ROOT),
            ],
            cwd=launch_root,
            env=environment,
            check=True,
        )
    for sdist in output.glob("*.tar.gz"):
        normalize_sdist(sdist, source_date_epoch=source_date_epoch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int)
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    epoch = args.source_date_epoch
    if epoch is None:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or _git_commit_epoch())

    with tempfile.TemporaryDirectory(prefix="iic-build-one-") as first_root:
        with tempfile.TemporaryDirectory(prefix="iic-build-two-") as second_root:
            first = Path(first_root)
            second = Path(second_root)
            _build(first, source_date_epoch=epoch)
            _build(second, source_date_epoch=epoch)
            hashes = compare_artifacts(first, second)
            for name in sorted(hashes):
                shutil.copy2(first / name, output / name)
    print(json.dumps({"source_date_epoch": epoch, "artifacts": hashes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
