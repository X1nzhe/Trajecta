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

import logging
from typing import Any, Literal

from backend.app import llm, rag, storage
from backend.app.ids import make_eval_case_id, make_success_case_id
from backend.app.schemas import EvalCase, EvidenceItem, FollowupSuggestion


logger = logging.getLogger(__name__)


_MAX_FOLLOWUP_SUGGESTIONS = 4
# Mirrors FollowupSuggestion field constraints (label 1..40, message
# 1..200). Defined here so the coercion helper can apply them without
# round-tripping through Pydantic on each over-long item.
_FOLLOWUP_LABEL_MAX = 40
_FOLLOWUP_MESSAGE_MAX = 200


def _coerce_followup_suggestions(
    raw: list[Any],
) -> list["FollowupSuggestion"]:
    """Coerce agent-emitted followup chips to the FollowupSuggestion shape.

    ``suggested_followups`` is transport-only UI metadata — chips
    rendered next to the input box. The agent occasionally emits an
    over-long label (e.g. 41 chars when the cap is 40), which used to
    fail strict ``FollowupSuggestion.model_validate`` and propagate
    out as a tool error that surfaced in the UI as
    "Verdict proposal — Tool error". Rejecting the entire verdict
    over one cosmetic field is the wrong tradeoff: silently trim the
    text to fit and skip items that are unrecoverable (empty after
    strip, wrong shape, etc.) so the verdict still lands cleanly.

    The agent doesn't need to retry — its over-long label was a
    near-miss and the truncated version is a perfectly usable chip.
    """

    validated: list[FollowupSuggestion] = []
    for item in raw:
        coerced = _coerce_one_followup(item)
        if coerced is not None:
            validated.append(coerced)
    return validated


def _coerce_one_followup(item: Any) -> "FollowupSuggestion | None":
    if isinstance(item, FollowupSuggestion):
        return item
    if not isinstance(item, dict):
        return None
    raw_label = item.get("label")
    raw_message = item.get("message")
    if not isinstance(raw_label, str) or not isinstance(raw_message, str):
        return None
    label = raw_label.strip()[:_FOLLOWUP_LABEL_MAX]
    message = raw_message.strip()[:_FOLLOWUP_MESSAGE_MAX]
    if not label or not message:
        return None
    if label != raw_label.strip() or message != raw_message.strip():
        logger.info(
            "Truncated overlong followup suggestion (label=%d→%d chars, message=%d→%d chars)",
            len(raw_label.strip()), len(label),
            len(raw_message.strip()), len(message),
        )
    return FollowupSuggestion(label=label, message=message)


def get_run(run_id: str) -> dict[str, Any]:
    """Load a trajectory run by ID, including its preprocessed digest if cached.

    Returns the full TrajectoryRun (task, status, steps with action/observation/result
    per step) plus an optional ``digest`` key carrying the cached TrajectoryDigest
    (low-detail VLM summaries per step). Call this once at the start of analysis
    to orient on the task and the per-step digest before deciding which steps
    to inspect at high detail.
    """

    run = storage.load_run(run_id)
    payload = run.model_dump(mode="json")
    digest = storage.load_digest(run_id)
    if digest is not None:
        payload["digest"] = digest.model_dump(mode="json")
    return payload


def find_similar_successful_run(
    task: str,
    top_k: int = 3,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve previously human-validated successful runs whose task is similar.

    Use this to find a counter-example for replay-and-diff once a likely failure
    region is identified. ``task`` is the natural-language task description from
    the run under analysis. ``exclude_run_id`` should be set to the current
    run_id to avoid returning the run itself. Returns up to ``top_k`` records,
    each with run_id / task / status / step_count. Empty list is normal when
    no validated success case exists yet.

    The ``run_id`` values returned here are NOT case_ids. Do NOT place them in
    ``propose_eval_case.retrieved_context_ids`` — that field only accepts
    case_ids from ``search_failure_memory`` or ``search_eval_cases``. Similar-
    run comparisons are tracked through the AgentTrace itself.
    """

    return rag.query_similar_successful_runs(
        task,
        top_k=top_k,
        exclude_run_id=exclude_run_id,
    )


def get_step_detail(
    run_id: str,
    step_index: int,
    image_detail: Literal["low", "high"] = "high",
) -> dict[str, Any]:
    """Inspect one step in depth, optionally invoking a high-detail VLM call on its screenshot.

    Returns the step's action / observation / result / coordinate_validation
    plus a ``vlm_summary`` string when a screenshot exists.

    ``image_detail`` controls VLM token cost: ``"high"`` (default, ~1500
    tokens) is required for any claim about visible text, button labels,
    target identity, or coordinate correctness; ``"low"`` (~85 tokens) is
    cheap orientation only and must not be cited as sole evidence in the
    final EvalCase.

    Use this on the most suspicious steps surfaced by the digest, not on
    every step — high-detail calls are budgeted.
    """

    try:
        run = storage.load_run(run_id)
    except FileNotFoundError:
        return {"tool_error": f"unknown run_id: {run_id}"}

    step = next((candidate for candidate in run.steps if candidate.index == step_index), None)
    if step is None:
        return {
            "tool_error": (
                f"step_index {step_index} not found for run {run_id} "
                f"with {len(run.steps)} stored steps"
            )
        }

    if image_detail not in {"low", "high"}:
        return {"tool_error": f"unsupported image_detail: {image_detail}"}

    screenshot_filename = step.observation.screenshot
    screenshot_bytes: bytes | None = None
    if screenshot_filename:
        screenshot_bytes = storage.load_screenshot(run_id, screenshot_filename)

    has_screenshot = screenshot_bytes is not None
    vlm_summary: str | None = None
    if has_screenshot:
        try:
            client = llm.get_vlm_client()
            image_name = screenshot_filename or f"step_{step_index}"
            if image_detail == "low":
                vlm_summary = client.summarize_low_detail(
                    screenshot_bytes,
                    image_name=image_name,
                    action_type=step.action.type,
                    step_index=step_index,
                )
            else:
                vlm_summary = client.summarize_high_detail(
                    screenshot_bytes,
                    image_name=image_name,
                    action_type=step.action.type,
                    step_index=step_index,
                )
        except Exception as exc:
            # Outer guard: RealVLMClient already logs OpenAI-side failures.
            # Anything bubbling up here is something else (bad screenshot
            # bytes, client constructor blew up, etc.) — surface it so we
            # never silently feed the agent vlm_summary=null without a clue.
            logger.warning(
                "get_step_detail VLM dispatch failed (run_id=%s, step_index=%s): %s: %s",
                run_id, step_index, type(exc).__name__, exc,
            )
            vlm_summary = None

    screenshot_url = None
    if has_screenshot and screenshot_filename is not None:
        screenshot_url = f"/api/runs/{run_id}/screenshots/{screenshot_filename}"

    return {
        "run_id": run_id,
        "step_index": step_index,
        "has_screenshot": has_screenshot,
        "image_detail": image_detail,
        "vlm_summary": vlm_summary,
        "action": step.action.model_dump(mode="json"),
        "observation": step.observation.model_dump(mode="json"),
        "result": step.result.model_dump(mode="json"),
        "coordinate_validation": step.coordinate_validation.model_dump(mode="json"),
        "screenshot_url": screenshot_url,
    }


def search_failure_memory(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Retrieve curated failure-pattern memory cases (FailureMemoryCase) similar to the query.

    These are reusable failure patterns (e.g., "missed_constraint",
    "early_terminated") with summary + fix_hint. Query with a short
    natural-language description of the failure mode you suspect, grounded
    in evidence you have observed. Returns up to ``top_k`` cases. Each
    case has a ``case_id`` you must include in EvalCase.retrieved_context_ids
    if you rely on it for the final eval case.
    """

    cases = rag.query_failure_memory(query, top_k=top_k)
    return [case.model_dump(mode="json") for case in cases]


def search_eval_cases(query: str, top_k: int = 3, only_validated: bool = True) -> list[dict[str, Any]]:
    """Retrieve prior human-validated EvalCase records similar to the query.

    Use this to find precedent — has the agent seen a similar failure on
    another run before, and if so, what regression rule was authored? With
    ``only_validated=True`` (default), only cases that survived human review
    are returned. The ``case_id`` of any case you rely on must appear in
    EvalCase.retrieved_context_ids.
    """

    cases = rag.query_eval_cases(query, top_k=top_k, only_validated=only_validated)
    return [case.model_dump(mode="json") for case in cases]


def propose_eval_case(
    run_id: str,
    evidence: list[EvidenceItem | dict[str, Any]],
    retrieved_context_ids: list[str],
    failure_step: int | None = None,
    failure_type: str | None = None,
    expected_behavior: str | None = None,
    actual_behavior: str | None = None,
    regression_rule: str | None = None,
    suggested_followups: list[FollowupSuggestion | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Terminal tool for the Eval Agent. Produces a draft EvalCase.

    Two valid call shapes:

    - **Failure case** — caller supplies all five failure fields. ID is
      generated by ``make_eval_case_id`` and may collide-suffix.
    - **Success case** — caller omits all five failure fields. ID is
      generated by ``make_success_case_id``. The case still carries
      ``evidence`` so the human reviewer sees why the agent ruled "no
      failure". v1 allows at most one success case per run.

    Half-populated calls raise via the EvalCase model_validator (XOR
    rule). The handler exposes that as HTTP 422.

    ``retrieved_context_ids`` must contain ONLY case_ids returned by
    ``search_failure_memory`` (``fm_*``) or ``search_eval_cases``
    (``ec_*``). Run_ids returned by ``find_similar_successful_run`` are
    NOT eligible — they live in their own namespace and are tracked via
    the AgentTrace. Including them here is rejected and forces a retry.

    ``suggested_followups`` is an optional list (up to 4) of short
    ``{label, message}`` pairs the agent thinks the user might want to
    ask next, grounded in what this trace actually surfaced. They are
    NOT persisted as part of the EvalCase — the frontend reads them
    off the latest propose_eval_case event's tool-call args to render
    chips. Exceeding the cap or violating per-item length bounds raises
    via the FollowupSuggestion schema.
    """

    run = storage.load_run(run_id)
    evidence_items = [EvidenceItem.model_validate(item) for item in evidence]

    failure_fields = (failure_step, failure_type, expected_behavior, actual_behavior, regression_rule)
    present = sum(1 for value in failure_fields if value is not None)
    if present == 0:
        case_id = make_success_case_id(run_id)
    elif present == 5:
        case_id = make_eval_case_id(run_id, failure_step, failure_type)
    else:
        # Let the EvalCase model_validator raise the canonical error so
        # tool callers see one consistent error shape.
        case_id = f"ec_{run_id}_invalid"

    case = EvalCase(
        case_id=case_id,
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
    payload = case.model_dump(mode="json")

    if suggested_followups:
        if len(suggested_followups) > _MAX_FOLLOWUP_SUGGESTIONS:
            raise ValueError(
                f"suggested_followups capped at {_MAX_FOLLOWUP_SUGGESTIONS}; "
                f"got {len(suggested_followups)}"
            )
        validated = _coerce_followup_suggestions(suggested_followups)
        # Transport-only: include in the tool's return payload so the
        # event lands in the trace and the frontend can read it. The
        # persisted EvalCase row never sees this list.
        if validated:
            payload["suggested_followups"] = [item.model_dump(mode="json") for item in validated]

    return payload
