from pathlib import Path

import yaml

from scripts.release_manifest import verify_container_pins


ROOT = Path(__file__).resolve().parents[2]


def test_ci_release_gates_are_blocking_and_content_addressed() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "continue-on-error" not in workflow
    assert "scripts/quality_ratchet.py check" in workflow
    assert "pip-audit==2.10.1" in workflow
    assert "tradingagents.ops.sbom" in workflow
    assert "scripts/verify_reproducible_artifacts.py" in workflow
    assert "scripts/release_manifest.py" in workflow
    assert "severity: HIGH,CRITICAL" in workflow
    assert "scanners: secret,misconfig" in workflow
    assert workflow.count(
        "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25"
    ) == 3


def test_all_default_container_inputs_are_digest_pinned() -> None:
    pins = verify_container_pins(ROOT)
    assert "python:" in pins["dockerfile_images"]
    assert "ghcr.io/astral-sh/uv:" in pins["dockerfile_images"]
    assert pins["redis_image"].startswith("redis:7.4.9-alpine3.21@sha256:")


def test_every_explicit_compose_build_has_release_labels() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    build_configs = [compose["x-app-common"]["build"], compose["x-backup-common"]["build"]]
    for name in ("volume-init", "operator-monitor", "dashboard"):
        service = compose["services"][name]
        assert "args" not in service
        build_configs.append(service["build"])
    for build in build_configs:
        assert build["args"] == {
            "APP_VERSION": "0.2.5",
            "BUILD_DATE": "${IIC_BUILD_DATE:-1970-01-01T00:00:00Z}",
            "SCHEMA_VERSION": "6",
            "VCS_REF": "${IIC_VCS_REF:-unknown}",
        }


def test_fault_drill_is_disposable_and_confirmation_guarded() -> None:
    script = (ROOT / "ops/fault-drill.sh").read_text(encoding="utf-8")
    assert "DESTROY IIC-FORGE BATCH10 DRILL" in script
    assert "iic-forge-batch10-drill" in script
    assert "docker compose down --volumes --remove-orphans" in script
    assert "IIC_KEEP_DRILL" in script


def test_soak_and_final_checklists_are_present() -> None:
    soak = (ROOT / "ops/soak.sh").read_text(encoding="utf-8")
    assert "259200" in soak
    assert "RUN IIC-FORGE 72H SOAK" in soak
    assert "scripts/soak_evidence.py evaluate" in soak
    for name in ("deployment", "rollback", "incident", "recovery"):
        checklist = ROOT / "ops/checklists" / f"{name}.md"
        assert checklist.is_file()
        assert "- [ ]" in checklist.read_text(encoding="utf-8")
