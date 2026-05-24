"""Deterministic Phase 2 tool implementations.

Placeholder warning for Phase 3:

``search_failure_memory``, ``search_eval_cases``, and
``find_similar_successful_run`` are Phase 2 fallbacks. Phase 3 MUST replace
them with ChromaDB-backed retrieval implementations introduced in Phase 3,
and remove the token-overlap scoring in this file; the local fallback must
not remain as a runtime path once the ``failure_memory`` / ``eval_cases`` /
``successful_runs`` collections exist. ``get_step_detail`` is intentionally
unimplemented here; the VLM step-detail tool is owned by Phase 3.

``propose_eval_case``'s contract responsibility to validate that every
``retrieved_context_id`` appears in a prior ``search_*`` tool result of the
current ``AgentTrace`` is deferred to Phase 3 — Phase 2 has no trace.
"""

from __future__ import annotations

import re
from typing import Any

from backend.app import storage
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
    query_tokens = _tokens(task)
    candidates = []
    for run in storage.list_runs():
        if run.status != "success":
            continue
        score = _score(query_tokens, run.task)
        candidates.append((score, run.run_id, run))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "run_id": run.run_id,
            "task": run.task,
            "status": run.status,
            "step_count": len(run.steps),
        }
        for _, _, run in candidates[: max(top_k, 0)]
    ]


def get_step_detail(run_id: str, step_index: int, image_detail: str = "high") -> dict[str, Any]:
    raise NotImplementedError("Phase 3 owns VLM step detail; Phase 2 does not call a VLM.")


def search_failure_memory(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked = []
    for case in storage.load_failure_memory():
        text = " ".join(
            [
                case.case_id,
                case.failure_type,
                case.summary,
                case.fix_hint or "",
                " ".join(case.tags),
                case.source_run_id or "",
            ]
        )
        ranked.append((_score(query_tokens, text), case.case_id, case))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [case.model_dump(mode="json") for score, _, case in ranked[: max(top_k, 0)] if score > 0 or not query_tokens]


def search_eval_cases(query: str, top_k: int = 3, only_validated: bool = True) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked = []
    for case in storage.load_eval_cases():
        if only_validated and not case.human_validated:
            continue
        text = " ".join(
            [
                case.case_id,
                case.source_run_id,
                case.task,
                case.failure_type,
                case.expected_behavior,
                case.actual_behavior,
                case.regression_rule,
                " ".join(case.retrieved_context_ids),
            ]
        )
        ranked.append((_score(query_tokens, text), case.case_id, case))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [case.model_dump(mode="json") for score, _, case in ranked[: max(top_k, 0)] if score > 0 or not query_tokens]


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


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 1}


def _score(query_tokens: set[str], text: str) -> int:
    if not query_tokens:
        return 0
    haystack = text.lower()
    text_tokens = _tokens(text)
    overlap = len(query_tokens & text_tokens)
    substring_hits = sum(1 for token in query_tokens if token in haystack)
    return overlap * 10 + substring_hits
