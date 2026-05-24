"""Identifier helpers for Trajecta artifacts."""

from __future__ import annotations

from backend.app import storage


def make_eval_case_id(
    run_id: str,
    failure_step: int,
    failure_type: str,
    storage_module=storage,
) -> str:
    """Generate a stable ID for a failure-style EvalCase.

    Tries ``ec_{run_id}_step_{failure_step}`` first; if that namespace is
    already taken (e.g., a re-analysis produced a second eval case for the
    same step), appends the failure_type for disambiguation.
    """

    base = f"ec_{run_id}_step_{failure_step}"
    if not storage_module.eval_case_exists(base):
        return base
    return f"{base}_{failure_type}"


def make_success_case_id(run_id: str, storage_module=storage) -> str:
    """Generate a stable ID for a success-style EvalCase.

    Lives in its own namespace from failure cases so the two cannot collide:
    failure cases are ``ec_{run_id}_step_*``, success cases are
    ``ec_{run_id}_success``. v1 allows at most one success case per run; the
    function raises if one already exists, leaving the caller to surface a
    409 instead of silently appending a suffix.
    """

    case_id = f"ec_{run_id}_success"
    if storage_module.eval_case_exists(case_id):
        raise FileExistsError(f"success eval case already exists for run {run_id!r}")
    return case_id
