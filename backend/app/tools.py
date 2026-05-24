"""Eval Agent tool implementations.

``search_failure_memory``, ``search_eval_cases``, and
``find_similar_successful_run`` delegate to ``rag.query_*`` against the
three v1 ChromaDB collections. The external signatures and JSON return
shapes are stable; agent code and existing API handlers do not change.

``get_step_detail`` remains owned by Phase 3c (VLM step detail).

``propose_eval_case``'s contract responsibility to validate that every
``retrieved_context_id`` appears in a prior ``search_*`` tool result of the
current ``AgentTrace`` is deferred to Phase 3d — there is still no trace
in scope here.
"""

from __future__ import annotations

from typing import Any

from backend.app import rag, storage
from backend.app.ids import make_eval_case_id
from backend.app.schemas import EvalCase, EvidenceItem


def get_run(run_id: str) -> dict[str, Any]:
    run = storage.load_run(run_id)
    payload = run.model_dump(mode="json")
    digest = storage.load_digest(run_id)
    if digest is not None:
        payload["digest"] = digest.model_dump(mode="json")
    return payload


def find_similar_successful_run(task: str, top_k: int = 3) -> list[dict[str, Any]]:
    return rag.query_similar_successful_runs(task, top_k=top_k)


def get_step_detail(run_id: str, step_index: int, image_detail: str = "high") -> dict[str, Any]:
    raise NotImplementedError("Phase 3 owns VLM step detail; Phase 2 does not call a VLM.")


def search_failure_memory(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    cases = rag.query_failure_memory(query, top_k=top_k)
    return [case.model_dump(mode="json") for case in cases]


def search_eval_cases(query: str, top_k: int = 3, only_validated: bool = True) -> list[dict[str, Any]]:
    cases = rag.query_eval_cases(query, top_k=top_k, only_validated=only_validated)
    return [case.model_dump(mode="json") for case in cases]


def propose_eval_case(
    run_id: str,
    failure_step: int,
    failure_type: str,
    expected_behavior: str,
    actual_behavior: str,
    evidence: list[EvidenceItem | dict[str, Any]],
    regression_rule: str,
    retrieved_context_ids: list[str],
) -> dict[str, Any]:
    run = storage.load_run(run_id)
    evidence_items = [EvidenceItem.model_validate(item) for item in evidence]
    case = EvalCase(
        case_id=make_eval_case_id(run_id, failure_step, failure_type),
        source_run_id=run_id,
        task=run.task,
        failure_step=failure_step,
        failure_type=failure_type,
        expected_behavior=expected_behavior,
        actual_behavior=actual_behavior,
        evidence=evidence_items,
        regression_rule=regression_rule,
        retrieved_context_ids=retrieved_context_ids,
        human_validated=False,
    )
    return case.model_dump(mode="json")
