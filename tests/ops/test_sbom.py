import hashlib
import json

from tradingagents.ops.sbom import generate_sbom


def test_sbom_is_deterministic_sorted_and_lock_bound(tmp_path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text("locked\n", encoding="utf-8")
    distributions = [
        {"name": "Zulu_Pkg", "version": "2.0"},
        {"name": "alpha.pkg", "version": "1.0"},
        {"name": "iic-forge", "version": "0.2.5"},
    ]
    first = generate_sbom(
        distributions,
        application_name="iic-forge",
        application_version="0.2.5",
        lockfile=lock,
    )
    second = generate_sbom(
        reversed(distributions),
        application_name="iic-forge",
        application_version="0.2.5",
        lockfile=lock,
    )
    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    assert first["specVersion"] == "1.6"
    assert [item["name"] for item in first["components"]] == ["alpha.pkg", "Zulu_Pkg"]
    properties = {item["name"]: item["value"] for item in first["metadata"]["properties"]}
    assert properties["iic-forge:lockfile:sha256"] == hashlib.sha256(b"locked\n").hexdigest()
    assert "timestamp" not in json.dumps(first)
