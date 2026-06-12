import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_focused_soak_gate_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/focused_soak_gate.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "Focused Soak Gate" in result.stdout or "--mode" in result.stdout


@pytest.mark.unit
def test_shadow_eval_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/shadow_eval.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--help" in result.stdout or "usage:" in result.stdout


@pytest.mark.unit
def test_f4_f5_exit_gate_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/f4_f5_exit_gate.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--help" in result.stdout or "usage:" in result.stdout


@pytest.mark.unit
def test_f5_exit_gate_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/f5_exit_gate.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--help" in result.stdout or "usage:" in result.stdout


@pytest.mark.unit
def test_f3_exit_gate_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/f3_exit_gate.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--help" in result.stdout or "usage:" in result.stdout


@pytest.mark.unit
def test_f4_exit_gate_help_runs_as_direct_script():
    result = subprocess.run(
        [sys.executable, "scripts/f4_exit_gate.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--help" in result.stdout or "usage:" in result.stdout


@pytest.mark.unit
def test_ensure_repo_root_on_path_is_idempotent():
    from scripts._repo_bootstrap import ensure_repo_root_on_path

    root = ensure_repo_root_on_path()
    assert root == ROOT
    ensure_repo_root_on_path()
    assert sys.path.count(str(root)) == 1
