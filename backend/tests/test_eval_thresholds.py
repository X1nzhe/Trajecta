"""Tests for the CI eval threshold gate (``scripts/check_eval_thresholds.py``).

Folds the metric-regression gate into the default pytest suite so it is
enforced locally and by CI's ``pytest`` step, not only the standalone
``eval-gate`` workflow job. The gate reads committed report artifacts; these
tests assert the happy path passes against the real tracked files and that an
impossible floor makes the gate bite. Invocation mirrors
``test_golden_set.py`` (subprocess + exit code) so we exercise the real CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_eval_thresholds.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_committed_metrics_clear_their_floors() -> None:
    result = _run()
    assert result.returncode == 0, (
        f"eval threshold gate failed against committed artifacts:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "eval threshold gate OK" in result.stdout


def test_impossible_faithfulness_floor_fails_loud() -> None:
    result = _run("--faithfulness-min", "0.999")
    assert result.returncode == 1
    assert "FAIL" in result.stderr
    assert "eval threshold gate FAILED" in result.stderr


def test_missing_artifact_fails_loud() -> None:
    result = _run("--report", "eval/runs/does-not-exist/agent_report.json")
    assert result.returncode == 1
    assert "not found" in result.stderr
