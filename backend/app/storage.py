"""Local-disk persistence for Trajecta backend artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from backend.app.schemas import (
    AgentTrace,
    EvalCase,
    FailureMemoryCase,
    TrajectoryDigest,
    TrajectoryRun,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")

ModelT = TypeVar("ModelT", bound=BaseModel)


def data_dir() -> Path:
    """Return the configured Trajecta data directory."""

    return Path(os.environ.get("TRAJECTA_DATA_DIR", REPO_ROOT / "data")).resolve()


def raw_sample_dir() -> Path:
    return data_dir() / "raw" / "molmoweb_humanskills_sample"


def _safe_id(value: str, *, kind: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


def _run_dir(run_id: str) -> Path:
    return data_dir() / "runs" / _safe_id(run_id, kind="run_id")


def screenshots_dir(run_id: str) -> Path:
    return _run_dir(run_id) / "screenshots"


def screenshot_path(run_id: str, filename: str) -> Path:
    base = screenshots_dir(run_id).resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"screenshot path escapes run screenshots directory: {filename!r}") from exc
    return target


def _run_artifact_path(run_id: str, filename: str) -> Path:
    return _run_dir(run_id) / filename


def _eval_case_path(case_id: str) -> Path:
    return data_dir() / "eval_cases" / "validated" / f"{_safe_id(case_id, kind='case_id')}.json"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_model(path: Path, model: BaseModel) -> None:
    payload = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, payload)


def _load_model(path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def load_run(run_id: str) -> TrajectoryRun:
    try:
        path = _run_artifact_path(run_id, "trajectory.json")
    except ValueError as exc:
        raise FileNotFoundError(f"unknown run_id: {run_id}") from exc
    if not path.exists():
        raise FileNotFoundError(f"unknown run_id: {run_id}")
    return _load_model(path, TrajectoryRun)


def save_run(run: TrajectoryRun) -> None:
    validated = TrajectoryRun.model_validate(run)
    _atomic_write_model(_run_artifact_path(validated.run_id, "trajectory.json"), validated)


def list_runs() -> list[TrajectoryRun]:
    runs_root = data_dir() / "runs"
    if not runs_root.exists():
        return []
    runs: list[TrajectoryRun] = []
    for path in sorted(runs_root.glob("*/trajectory.json")):
        runs.append(_load_model(path, TrajectoryRun))
    return runs


def run_exists(run_id: str) -> bool:
    try:
        return _run_artifact_path(run_id, "trajectory.json").exists()
    except ValueError:
        return False


def load_digest(run_id: str) -> TrajectoryDigest | None:
    try:
        path = _run_artifact_path(run_id, "digest.json")
    except ValueError:
        return None
    if not path.exists():
        return None
    return _load_model(path, TrajectoryDigest)


def save_digest(run_id: str, digest: TrajectoryDigest) -> None:
    validated = TrajectoryDigest.model_validate(digest)
    _atomic_write_model(_run_artifact_path(run_id, "digest.json"), validated)


def delete_digest(run_id: str) -> None:
    try:
        _run_artifact_path(run_id, "digest.json").unlink(missing_ok=True)
    except ValueError:
        return


def load_trace(run_id: str) -> AgentTrace | None:
    try:
        path = _run_artifact_path(run_id, "last_trace.json")
    except ValueError:
        return None
    if not path.exists():
        return None
    return _load_model(path, AgentTrace)


def save_trace(run_id: str, trace: AgentTrace) -> None:
    validated = AgentTrace.model_validate(trace)
    _atomic_write_model(_run_artifact_path(run_id, "last_trace.json"), validated)


def save_eval_case(case: EvalCase) -> None:
    validated = EvalCase.model_validate(case)
    path = _eval_case_path(validated.case_id)
    if path.exists():
        raise FileExistsError(f"eval case already exists: {validated.case_id}")
    _atomic_write_model(path, validated)


def load_eval_case(case_id: str) -> EvalCase | None:
    try:
        path = _eval_case_path(case_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    return _load_model(path, EvalCase)


def load_eval_cases() -> list[EvalCase]:
    cases_root = data_dir() / "eval_cases" / "validated"
    if not cases_root.exists():
        return []
    return [_load_model(path, EvalCase) for path in sorted(cases_root.glob("*.json"))]


def eval_case_exists(case_id: str) -> bool:
    try:
        return _eval_case_path(case_id).exists()
    except ValueError:
        return False


def load_failure_memory() -> list[FailureMemoryCase]:
    path = data_dir() / "failure_memory" / "cases.jsonl"
    if not path.exists():
        return []

    cases: list[FailureMemoryCase] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = FailureMemoryCase.model_validate_json(line)
        if case.case_id in seen:
            raise ValueError(f"duplicate failure memory case_id {case.case_id!r} at line {line_no}")
        seen.add(case.case_id)
        cases.append(case)
    return cases


def save_screenshots(run_id: str, screenshots: Mapping[str, bytes]) -> None:
    for filename, data in screenshots.items():
        _atomic_write_bytes(screenshot_path(run_id, filename), data)
