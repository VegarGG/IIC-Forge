#!/usr/bin/env python3
"""Generate a deterministic manifest for one reviewed IIC-Forge release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REFERENCE_PATTERN = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_container_pins(root: Path) -> dict[str, str]:
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    image_args = re.findall(r"^ARG\s+([A-Z_]*IMAGE)=([^\s]+)$", dockerfile, re.MULTILINE)
    missing = [f"{name}={value}" for name, value in image_args if "@sha256:" not in value]
    if missing:
        raise ValueError("Dockerfile image inputs are not digest-pinned: " + ", ".join(missing))

    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"IIC_REDIS_IMAGE:-([^}]+)", compose)
    if match is None or "@sha256:" not in match.group(1):
        raise ValueError("the default Redis image is not digest-pinned")
    return {
        "dockerfile_images": ",".join(f"{name}={value}" for name, value in image_args),
        "redis_image": match.group(1),
    }


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _created_utc(source_date_epoch: int) -> str:
    return datetime.fromtimestamp(source_date_epoch, tz=timezone.utc).isoformat()


def build_manifest(
    *,
    root: Path,
    artifacts: Iterable[Path],
    image_reference: str,
    image_id: str,
    commit: str,
    source_date_epoch: int,
) -> dict[str, Any]:
    if not (REFERENCE_PATTERN.fullmatch(image_reference) or DIGEST_PATTERN.fullmatch(image_id)):
        raise ValueError("provide a digest image reference or a sha256 image ID")
    pins = verify_container_pins(root)
    artifact_rows = []
    for artifact in sorted((path.resolve(strict=True) for path in artifacts), key=lambda p: p.name):
        artifact_rows.append(
            {"name": artifact.name, "size_bytes": artifact.stat().st_size, "sha256": sha256_file(artifact)}
        )
    migrations = sorted((root / "tradingagents" / "persistence" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise ValueError("no packaged schema migrations found")
    schema_version = int(migrations[-1].name.split("_", 1)[0])
    input_files = ("pyproject.toml", "uv.lock", "Dockerfile", "docker-compose.yml")
    return {
        "schema_version": 1,
        "created_utc": _created_utc(source_date_epoch),
        "source": {
            "commit": commit,
            "source_date_epoch": source_date_epoch,
            "tree": _git(root, "rev-parse", f"{commit}^{{tree}}"),
        },
        "application": {"schema_version": schema_version},
        "image": {"reference": image_reference, "id": image_id, **pins},
        "inputs": {
            name: {"sha256": sha256_file(root / name)} for name in input_files
        },
        "artifacts": artifact_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", type=Path, required=True)
    parser.add_argument("--image-reference", default="")
    parser.add_argument("--image-id", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    # Build outputs and downloaded evidence are intentionally untracked.  Only
    # tracked source drift would make the commit/tree identity misleading.
    if _git(ROOT, "status", "--porcelain", "--untracked-files=no"):
        parser.error("release manifests require no tracked Git worktree changes")
    commit = _git(ROOT, "rev-parse", "HEAD")
    epoch_text = os.environ.get("SOURCE_DATE_EPOCH") or _git(ROOT, "show", "-s", "--format=%ct", commit)
    manifest = build_manifest(
        root=ROOT,
        artifacts=args.artifact,
        image_reference=args.image_reference,
        image_id=args.image_id,
        commit=commit,
        source_date_epoch=int(epoch_text),
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"release manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
