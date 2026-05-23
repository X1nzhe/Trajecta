"""Identifier helpers for Trajecta artifacts."""

from __future__ import annotations

from backend.app import storage


def make_eval_case_id(
    run_id: str,
    failure_step: int,
    failure_type: str,
    storage_module=storage,
) -> str:
    base = f"ec_{run_id}_step_{failure_step}"
    if not storage_module.eval_case_exists(base):
        return base
    return f"{base}_{failure_type}"
