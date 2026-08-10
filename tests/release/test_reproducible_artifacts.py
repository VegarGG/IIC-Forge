import gzip
import io
import tarfile
from pathlib import Path

import pytest

from scripts.verify_reproducible_artifacts import compare_artifacts, normalize_sdist


def _artifact(root: Path, name: str, content: bytes) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(content)


def test_compare_artifacts_accepts_identical_wheel_and_sdist(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        _artifact(root, "iic.whl", b"wheel")
        _artifact(root, "iic.tar.gz", b"sdist")
    assert set(compare_artifacts(first, second)) == {"iic.whl", "iic.tar.gz"}


def test_compare_artifacts_rejects_byte_drift(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _artifact(first, "iic.whl", b"first")
    _artifact(second, "iic.whl", b"second")
    with pytest.raises(ValueError, match="non-reproducible"):
        compare_artifacts(first, second)


def _sdist(path: Path, *, timestamp: int, owner: str) -> None:
    with path.open("wb") as destination:
        with gzip.GzipFile(
            filename=path.name, mode="wb", fileobj=destination, mtime=timestamp
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo("iic_forge-0.2.5/PKG-INFO")
                member.size = len(b"same payload")
                member.mtime = timestamp + 0.125
                member.uname = owner
                member.gname = "staff"
                archive.addfile(member, io.BytesIO(b"same payload"))


def test_normalize_sdist_removes_host_and_time_metadata(tmp_path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _sdist(first, timestamp=100, owner="first-user")
    _sdist(second, timestamp=200, owner="second-user")

    normalize_sdist(first, source_date_epoch=42)
    normalize_sdist(second, source_date_epoch=42)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        member = archive.getmember("iic_forge-0.2.5/PKG-INFO")
        assert member.mtime == 42
        assert member.uid == 0
        assert member.gid == 0
        assert member.uname == ""
        assert member.gname == ""
        assert archive.extractfile(member).read() == b"same payload"
