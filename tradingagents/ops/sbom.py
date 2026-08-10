"""Deterministic CycloneDX inventory for an installed IIC-Forge environment."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _component(name: str, version: str) -> dict[str, str]:
    canonical = _canonical_name(name)
    encoded_name = quote(canonical, safe="-._~")
    encoded_version = quote(version, safe="-._~+")
    return {
        "type": "library",
        "bom-ref": f"pkg:pypi/{encoded_name}@{encoded_version}",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{encoded_name}@{encoded_version}",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_sbom(
    distributions: Iterable[Mapping[str, str]],
    *,
    application_name: str,
    application_version: str,
    lockfile: Path | None = None,
) -> dict[str, Any]:
    """Create stable CycloneDX 1.6 JSON without timestamps or random serials."""
    app_key = _canonical_name(application_name)
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for distribution in distributions:
        name = str(distribution.get("name") or "").strip()
        version = str(distribution.get("version") or "").strip()
        if not name or not version or _canonical_name(name) == app_key:
            continue
        unique[(_canonical_name(name), version)] = _component(name, version)

    properties: list[dict[str, str]] = []
    if lockfile is not None:
        resolved = lockfile.expanduser().resolve(strict=True)
        properties.extend(
            (
                {"name": "iic-forge:lockfile", "value": resolved.name},
                {"name": "iic-forge:lockfile:sha256", "value": _sha256(resolved)},
            )
        )

    application = _component(application_name, application_version)
    application["type"] = "application"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": application,
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "iic-forge-sbom",
                        "version": "1",
                    }
                ]
            },
            "properties": properties,
        },
        "components": [unique[key] for key in sorted(unique)],
    }


def installed_distributions() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip()
        if name:
            result.append({"name": name, "version": distribution.version})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-name", default="iic-forge")
    parser.add_argument("--application-version")
    parser.add_argument("--lockfile", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    version = args.application_version
    if not version:
        try:
            version = importlib.metadata.version(args.application_name)
        except importlib.metadata.PackageNotFoundError as exc:
            parser.error(
                "--application-version is required outside an installed environment"
            )
            raise AssertionError("unreachable") from exc
    payload = generate_sbom(
        installed_distributions(),
        application_name=args.application_name,
        application_version=version,
        lockfile=args.lockfile,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
