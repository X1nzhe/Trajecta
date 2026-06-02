"""SQLite-backed persistence for Trajecta backend artifacts.

Public function signatures are stable across the v1 filesystem → SQLite cutover;
only the implementation changed. Two behavioral notes:

- ``screenshot_path`` and ``screenshots_dir`` were removed. Screenshots live
  inside the database as BLOBs; ``load_screenshot(trajectory_id, filename)`` returns
  ``bytes | None`` and is the only access path. Callers that previously read
  files from disk now read bytes from the DB.
- ``data_dir()`` still resolves ``TRAJECTA_DATA_DIR`` and is exported because
  ``rag.py`` uses it to locate the Chroma persist directory.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import delete, select

from backend.app import db, models
from backend.app.schemas import (
    AgentTrace,
    EvalCase,
    FailureMemoryCase,
    TrajectoryDigest,
    Trajectory,
    TrajectoryStep,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")


def data_dir() -> Path:
    return Path(os.environ.get("TRAJECTA_DATA_DIR", REPO_ROOT / "data")).resolve()


def raw_sample_dir() -> Path:
    return data_dir() / "raw" / "molmoweb_humanskills_sample"


def _safe_id(value: str, *, kind: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Trajectories + Steps
# ---------------------------------------------------------------------------


def _trajectory_to_orm(trajectory: Trajectory) -> tuple[models.Trajectory, list[models.Step]]:
    trajectory_row = models.Trajectory(
        trajectory_id=trajectory.trajectory_id,
        task=trajectory.task,
        source=trajectory.source,
        status=trajectory.status,
        trajectory_metadata=dict(trajectory.metadata),
    )
    step_rows = [
        models.Step(
            trajectory_id=trajectory.trajectory_id,
            step_index=step.index,
            timestamp=step.timestamp,
            observation_json=step.observation.model_dump(mode="json"),
            action_json=step.action.model_dump(mode="json"),
            result_json=step.result.model_dump(mode="json"),
            coordinate_validation_json=step.coordinate_validation.model_dump(mode="json"),
            step_metadata=dict(step.metadata),
        )
        for step in trajectory.steps
    ]
    return trajectory_row, step_rows


def _orm_to_trajectory(trajectory_row: models.Trajectory) -> Trajectory:
    return Trajectory(
        trajectory_id=trajectory_row.trajectory_id,
        task=trajectory_row.task,
        source=trajectory_row.source,
        status=trajectory_row.status,
        metadata=dict(trajectory_row.trajectory_metadata or {}),
        steps=[
            TrajectoryStep.model_validate(
                {
                    "index": step.step_index,
                    "timestamp": step.timestamp,
                    "observation": step.observation_json,
                    "action": step.action_json,
                    "result": step.result_json,
                    "coordinate_validation": step.coordinate_validation_json,
                    "metadata": dict(step.step_metadata or {}),
                }
            )
            for step in trajectory_row.steps
        ],
    )


def save_trajectory(trajectory: Trajectory) -> None:
    validated = Trajectory.model_validate(trajectory)
    _safe_id(validated.trajectory_id, kind="trajectory_id")
    with db.session_scope() as session:
        existing = session.get(models.Trajectory, validated.trajectory_id)
        trajectory_row, step_rows = _trajectory_to_orm(validated)
        if existing is not None:
            # Update in place + replace only Step rows. Deleting the Trajectory row
            # would cascade into screenshots/digest/trace and silently wipe
            # the user's prior analysis (docs/dataset_import.md "Re-Import
            # Behavior" promises traces survive re-import).
            existing.task = trajectory_row.task
            existing.source = trajectory_row.source
            existing.status = trajectory_row.status
            existing.trajectory_metadata = trajectory_row.trajectory_metadata
            existing.steps.clear()  # delete-orphan flushes the old Step rows
            session.flush()
            existing.steps.extend(step_rows)
        else:
            trajectory_row.steps = step_rows
            session.add(trajectory_row)


def load_trajectory(trajectory_id: str) -> Trajectory:
    try:
        _safe_id(trajectory_id, kind="trajectory_id")
    except ValueError as exc:
        raise FileNotFoundError(f"unknown trajectory_id: {trajectory_id}") from exc
    with db.session_scope() as session:
        row = session.get(models.Trajectory, trajectory_id)
        if row is None:
            raise FileNotFoundError(f"unknown trajectory_id: {trajectory_id}")
        return _orm_to_trajectory(row)


def list_trajectories() -> list[Trajectory]:
    with db.session_scope() as session:
        stmt = select(models.Trajectory).order_by(models.Trajectory.trajectory_id)
        return [_orm_to_trajectory(row) for row in session.scalars(stmt).all()]


def trajectory_exists(trajectory_id: str) -> bool:
    try:
        _safe_id(trajectory_id, kind="trajectory_id")
    except ValueError:
        return False
    with db.session_scope() as session:
        return session.get(models.Trajectory, trajectory_id) is not None


# ---------------------------------------------------------------------------
# Screenshots (BLOB)
# ---------------------------------------------------------------------------


def save_screenshots(trajectory_id: str, screenshots: Mapping[str, bytes]) -> None:
    _safe_id(trajectory_id, kind="trajectory_id")
    if not screenshots:
        return
    with db.session_scope() as session:
        if session.get(models.Trajectory, trajectory_id) is None:
            raise FileNotFoundError(f"cannot attach screenshots; unknown trajectory_id: {trajectory_id}")
        for filename, data in screenshots.items():
            _safe_id(filename, kind="screenshot_filename")
            existing = session.get(models.Screenshot, (trajectory_id, filename))
            if existing is None:
                session.add(
                    models.Screenshot(
                        trajectory_id=trajectory_id,
                        filename=filename,
                        content_type=_infer_content_type(filename),
                        data=data,
                    )
                )
            else:
                existing.data = data
                existing.content_type = _infer_content_type(filename)


def load_screenshot(trajectory_id: str, filename: str) -> bytes | None:
    result = load_screenshot_with_meta(trajectory_id, filename)
    return result[0] if result is not None else None


def screenshot_content_type(trajectory_id: str, filename: str) -> str | None:
    result = load_screenshot_with_meta(trajectory_id, filename)
    return result[1] if result is not None else None


def load_screenshot_with_meta(trajectory_id: str, filename: str) -> tuple[bytes, str] | None:
    """Single-query screenshot fetch. Returns ``(data, content_type)`` or ``None``."""

    try:
        _safe_id(trajectory_id, kind="trajectory_id")
        _safe_id(filename, kind="screenshot_filename")
    except ValueError:
        return None
    with db.session_scope() as session:
        row = session.get(models.Screenshot, (trajectory_id, filename))
        if row is None:
            return None
        return row.data, row.content_type


def screenshot_exists(trajectory_id: str, filename: str) -> bool:
    try:
        _safe_id(trajectory_id, kind="trajectory_id")
        _safe_id(filename, kind="screenshot_filename")
    except ValueError:
        return False
    with db.session_scope() as session:
        return session.get(models.Screenshot, (trajectory_id, filename)) is not None


def _infer_content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def load_digest(trajectory_id: str) -> TrajectoryDigest | None:
    try:
        _safe_id(trajectory_id, kind="trajectory_id")
    except ValueError:
        return None
    with db.session_scope() as session:
        row = session.get(models.Digest, trajectory_id)
        if row is None:
            return None
        return TrajectoryDigest.model_validate(row.payload_json)


def save_digest(trajectory_id: str, digest: TrajectoryDigest) -> None:
    validated = TrajectoryDigest.model_validate(digest)
    if validated.trajectory_id != trajectory_id:
        raise ValueError(f"digest.trajectory_id {validated.trajectory_id!r} does not match trajectory_id argument {trajectory_id!r}")
    _safe_id(trajectory_id, kind="trajectory_id")
    with db.session_scope() as session:
        if session.get(models.Trajectory, trajectory_id) is None:
            raise FileNotFoundError(f"cannot attach digest; unknown trajectory_id: {trajectory_id}")
        existing = session.get(models.Digest, trajectory_id)
        payload = validated.model_dump(mode="json")
        if existing is None:
            session.add(models.Digest(trajectory_id=trajectory_id, payload_json=payload))
        else:
            existing.payload_json = payload


def delete_digest(trajectory_id: str) -> None:
    try:
        _safe_id(trajectory_id, kind="trajectory_id")
    except ValueError:
        return
    with db.session_scope() as session:
        existing = session.get(models.Digest, trajectory_id)
        if existing is not None:
            session.delete(existing)


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


def load_trace(trajectory_id: str) -> AgentTrace | None:
    try:
        _safe_id(trajectory_id, kind="trajectory_id")
    except ValueError:
        return None
    with db.session_scope() as session:
        row = session.get(models.Trace, trajectory_id)
        if row is None:
            return None
        return AgentTrace.model_validate(row.payload_json)


def save_trace(trajectory_id: str, trace: AgentTrace) -> None:
    validated = AgentTrace.model_validate(trace)
    if validated.trajectory_id != trajectory_id:
        raise ValueError(f"trace.trajectory_id {validated.trajectory_id!r} does not match trajectory_id argument {trajectory_id!r}")
    _safe_id(trajectory_id, kind="trajectory_id")
    with db.session_scope() as session:
        if session.get(models.Trajectory, trajectory_id) is None:
            raise FileNotFoundError(f"cannot attach trace; unknown trajectory_id: {trajectory_id}")
        existing = session.get(models.Trace, trajectory_id)
        payload = validated.model_dump(mode="json")
        if existing is None:
            session.add(models.Trace(trajectory_id=trajectory_id, payload_json=payload))
        else:
            existing.payload_json = payload


# ---------------------------------------------------------------------------
# Eval cases
# ---------------------------------------------------------------------------


def save_eval_case(case: EvalCase) -> None:
    validated = EvalCase.model_validate(case)
    _safe_id(validated.case_id, kind="case_id")
    with db.session_scope() as session:
        if session.get(models.EvalCaseRow, validated.case_id) is not None:
            raise FileExistsError(f"eval case already exists: {validated.case_id}")
        session.add(
            models.EvalCaseRow(
                case_id=validated.case_id,
                source_trajectory_id=validated.source_trajectory_id,
                payload_json=validated.model_dump(mode="json"),
                human_validated=validated.human_validated,
            )
        )


def load_eval_case(case_id: str) -> EvalCase | None:
    try:
        _safe_id(case_id, kind="case_id")
    except ValueError:
        return None
    with db.session_scope() as session:
        row = session.get(models.EvalCaseRow, case_id)
        if row is None:
            return None
        return EvalCase.model_validate(row.payload_json)


def load_eval_cases() -> list[EvalCase]:
    with db.session_scope() as session:
        stmt = select(models.EvalCaseRow).order_by(models.EvalCaseRow.case_id)
        return [EvalCase.model_validate(row.payload_json) for row in session.scalars(stmt).all()]


def eval_case_exists(case_id: str) -> bool:
    try:
        _safe_id(case_id, kind="case_id")
    except ValueError:
        return False
    with db.session_scope() as session:
        return session.get(models.EvalCaseRow, case_id) is not None


# ---------------------------------------------------------------------------
# Failure memory (seed corpus on disk; hydrated into DB on first read)
# ---------------------------------------------------------------------------


def load_failure_memory() -> list[FailureMemoryCase]:
    """Return the seed failure-memory corpus.

    Source of truth is still ``data/failure_memory/cases.jsonl`` (a curated,
    hand-edited file). On first read we hydrate it into the DB so the rest of
    the app sees a uniform persistence layer. The DB copy is rebuilt every
    call so edits to the JSONL file take effect without a manual migration.
    """

    path = data_dir() / "failure_memory" / "cases.jsonl"
    cases: list[FailureMemoryCase] = []
    seen: set[str] = set()
    if path.exists():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            case = FailureMemoryCase.model_validate_json(line)
            if case.case_id in seen:
                raise ValueError(f"duplicate failure memory case_id {case.case_id!r} at line {line_no}")
            seen.add(case.case_id)
            cases.append(case)

    with db.session_scope() as session:
        session.execute(delete(models.FailureMemoryRow))
        for case in cases:
            session.add(
                models.FailureMemoryRow(
                    case_id=case.case_id,
                    payload_json=case.model_dump(mode="json"),
                )
            )

    return cases
