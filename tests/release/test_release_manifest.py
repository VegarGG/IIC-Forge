import hashlib
from pathlib import Path

import pytest

from scripts.release_manifest import build_manifest, verify_container_pins


PIN = "sha256:" + "a" * 64


def _root(tmp_path: Path, *, redis_pinned: bool = True) -> Path:
    (tmp_path / "tradingagents/persistence/migrations").mkdir(parents=True)
    (tmp_path / "tradingagents/persistence/migrations/0006_last.sql").write_text("SELECT 1;\n")
    (tmp_path / "Dockerfile").write_text(f"ARG PYTHON_IMAGE=python@{PIN}\nFROM ${{PYTHON_IMAGE}}\n")
    redis = f"redis@{PIN}" if redis_pinned else "redis:latest"
    (tmp_path / "docker-compose.yml").write_text(f"image: ${{IIC_REDIS_IMAGE:-{redis}}}\n")
    for name in ("pyproject.toml", "uv.lock"):
        (tmp_path / name).write_text(name + "\n")
    return tmp_path


def test_verify_container_pins_rejects_mutable_redis(tmp_path) -> None:
    with pytest.raises(ValueError, match="Redis"):
        verify_container_pins(_root(tmp_path, redis_pinned=False))


def test_build_manifest_hashes_inputs_and_artifacts(tmp_path, monkeypatch) -> None:
    root = _root(tmp_path)
    artifact = tmp_path / "iic.whl"
    artifact.write_bytes(b"wheel")
    monkeypatch.setattr("scripts.release_manifest._git", lambda *_args: "tree-sha")
    result = build_manifest(
        root=root,
        artifacts=[artifact],
        image_reference="",
        image_id=PIN,
        commit="b" * 40,
        source_date_epoch=0,
    )
    assert result["created_utc"] == "1970-01-01T00:00:00+00:00"
    assert result["application"]["schema_version"] == 6
    assert result["artifacts"] == [
        {
            "name": "iic.whl",
            "size_bytes": 5,
            "sha256": hashlib.sha256(b"wheel").hexdigest(),
        }
    ]
