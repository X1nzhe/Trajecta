"""Manual RAGAS evaluation over persisted agent traces.

Run as: ``python -m backend.app.ragas_eval``.

Reads every persisted ``AgentTrace``, keeps the ones that terminated
via ``propose_eval_case``, and builds RAGAS faithfulness samples from
the actual RAG tool calls recorded in the trace. Retrieval is **not**
re-run: the trace's ``search_failure_memory`` / ``search_eval_cases``
tool-call queries and tool-result items are the evidence pool (see
``docs/testing.md`` and ``docs/eval_agent.md`` Observability).

## Trace sources (Phase 8 A6.1)

Trajecta has two on-disk locations that may carry persisted traces.
``collect_samples`` reads both in this precedence order **per run_id**:

1. ``--trace-dir <path>/<run_id>.json`` — the per-sample dumps produced
   by ``python -m backend.app.agent_eval --trace-dir …`` (Phase 8 A2).
   Explicit trace dirs bind A6 to the same formal eval artefact set.
2. The SQLite ``traces`` table — populated by the UI/API
   ``analyze_run`` flow and accessed via ``storage.load_trace``.

The run-id set graded is the **union** of run_ids visible in
``storage.list_runs()`` and any ``*.json`` files under the supplied
trace dir. The pre-storage-refactor ``data/runs/<id>/last_trace.json``
path is no longer read — that layout was retired by the storage
migration in Phase 6.

## Execution modes

- ``real``: uses the ``ragas`` package's ``faithfulness`` metric over
  no-ground-truth samples. Requires ``OPENAI_API_KEY`` and ``ragas`` to
  be importable.
- ``stub``: pure-Python stand-ins for both metrics that require neither
  a key nor a network. Selected automatically when ``ragas`` is missing
  or no key is present. Always writes the same two report files so the
  acceptance criterion (``eval/ragas_report.md`` exists) is met offline.

Stub heuristics — intentionally crude, see ``docs/testing.md``:

- ``faithfulness_stub``: fraction of evidence ``claim``s whose lower-cased
  tokens overlap ≥ 50% with any retrieved context string.
"""

from __future__ import annotations

import argparse
import json
import os
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.app import storage
from backend.app.schemas import AgentTrace, AgentTraceEvent


SEARCH_TOOL_NAMES = {"search_failure_memory", "search_eval_cases"}
TERMINAL_TOOL = "propose_eval_case"
GROUND_TRUTH_SOURCE_NONE = "none"


@dataclass
class RagasSample:
    run_id: str
    question: str
    answer: str
    contexts: list[str]
    ground_truth_source: str  # "none"
    proposed_failure_type: str
    retrieved_context_ids: list[str]
    tool_name: str
    tool_query: str


@dataclass
class SkippedCounts:
    budget_exceeded: int = 0
    error: int = 0
    no_trace: int = 0
    no_context: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "budget_exceeded": self.budget_exceeded,
            "error": self.error,
            "no_trace": self.no_trace,
            "no_context": self.no_context,
        }


@dataclass
class RagasReport:
    samples: list[dict[str, Any]] = field(default_factory=list)
    metric_means: dict[str, float] = field(default_factory=dict)
    skipped: SkippedCounts = field(default_factory=SkippedCounts)
    ground_truth_source: str = GROUND_TRUTH_SOURCE_NONE
    ragas_mode: str = "stub"  # "real" | "stub"
    fallback_reason: str | None = None


def ragas_answer_from_trace(trace: AgentTrace) -> str:
    """Concatenate the latest propose_eval_case args' actual_behavior with
    each evidence claim. Raises when the trace contains no terminal call.
    """

    if trace.terminated_by != TERMINAL_TOOL:
        raise ValueError(f"trace did not terminate via {TERMINAL_TOOL}")

    calls = [
        e for e in trace.events
        if e.type == "tool_call" and e.name == TERMINAL_TOOL
    ]
    if not calls:
        raise ValueError("trace has no propose_eval_case tool call")
    args = calls[-1].args or {}
    actual_behavior = args["actual_behavior"]
    evidence = args.get("evidence", [])
    claims = [item["claim"] for item in evidence]
    return actual_behavior + "\n\n" + "\n".join(claims)


def _latest_proposal(trace: AgentTrace) -> AgentTraceEvent | None:
    for event in reversed(trace.events):
        if event.type == "tool_call" and event.name == TERMINAL_TOOL:
            return event
    return None


def _context_text_from_item(item: dict[str, Any]) -> str:
    case_id = item.get("case_id") or item.get("run_id") or ""
    summary = item.get("summary") or item.get("task") or ""
    tags = item.get("tags") or []
    tag_str = ",".join(tags) if isinstance(tags, list) else ""
    return f"{case_id}: {summary} [{tag_str}]".strip()


def _contexts_from_tool_result(event: AgentTraceEvent) -> list[str]:
    payload = event.result or {}
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [_context_text_from_item(item) for item in items if isinstance(item, dict)]


def _iter_rag_tool_samples(trace: AgentTrace) -> list[tuple[str, str, list[str]]]:
    """Return ``(tool_name, query, contexts)`` rows from actual RAG calls."""

    samples: list[tuple[str, str, list[str]]] = []
    events = trace.events
    for index, event in enumerate(events):
        if event.type != "tool_call" or event.name not in SEARCH_TOOL_NAMES:
            continue
        args = event.args or {}
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            continue
        contexts: list[str] = []
        for candidate in events[index + 1:]:
            if candidate.type == "tool_result" and candidate.name == event.name:
                contexts = _contexts_from_tool_result(candidate)
                break
            if candidate.type == "tool_call":
                break
        samples.append((event.name or "", query.strip(), contexts))
    return samples


def load_trace_for_run_id(
    run_id: str,
    *,
    trace_dir: Path | None = None,
) -> AgentTrace | None:
    """Resolve one ``AgentTrace`` from the Phase 8 sources.

    Precedence (per ``docs/phase8_s18_alignment.md`` A6.1):

      1. ``trace_dir/<run_id>.json`` — the per-sample dump produced
         by ``agent_eval --trace-dir`` when a trace dir is supplied.
      2. ``storage.load_trace(run_id)`` — the SQLite ``traces`` row
         that ``analyze_run`` writes.

    Returns ``None`` when neither source carries a trace. Raises
    ``ValidationError`` only when the trace-dir file exists but does not
    parse — the caller is expected to count those as ``error`` skips.
    """
    if trace_dir is not None:
        path = trace_dir / f"{run_id}.json"
        if path.exists():
            return AgentTrace.model_validate_json(path.read_text(encoding="utf-8"))
    return storage.load_trace(run_id)


def _discover_run_ids(*, trace_dir: Path | None) -> list[str]:
    """Enumerate every run_id with a persisted trace worth grading.

    The discovery set is the **union** of SQLite-resident runs and any
    ``*.json`` files under the supplied trace dir. Returning a sorted
    list keeps the eval deterministic across invocations and across
    operating systems with different `iterdir` ordering.
    """
    run_ids: set[str] = set()
    try:
        run_ids.update(run.run_id for run in storage.list_runs())
    except Exception:
        # storage.list_runs hits the SQLite DB. A missing data dir or
        # a fresh checkout is a valid no-op — the eval still runs over
        # the trace-dir fallback if one was supplied.
        pass
    if trace_dir is not None and trace_dir.is_dir():
        for path in trace_dir.glob("*.json"):
            if path.is_file():
                run_ids.add(path.stem)
    return sorted(run_ids)


def collect_samples(
    data_root: Path | None = None,
    *,
    trace_dir: Path | None = None,
    limit: int | None = None,
) -> tuple[list[RagasSample], SkippedCounts]:
    """Collect RAGAS samples from persisted traces.

    See the module docstring (§ Trace sources) for the per-run_id
    precedence rule. ``data_root`` is retained for CLI compatibility but
    no longer contributes a ground-truth label: the formal A6 metric is
    no-ground-truth faithfulness over retrieved contexts.

    Counts skipped traces in three buckets:

      * ``budget_exceeded`` — trace terminated via the budget guardrail.
      * ``error`` — trace did not terminate via ``propose_eval_case``
        (or via the budget guardrail), failed validation, or had no
        terminal-tool args.
      * ``no_trace`` — neither SQLite nor the trace dir held a trace
        for that run_id.
      * ``no_context`` — a terminal trace had no usable RAG tool result
        contexts for faithfulness scoring.
    """
    samples: list[RagasSample] = []
    skipped = SkippedCounts()

    for run_id in _discover_run_ids(trace_dir=trace_dir):
        try:
            trace = load_trace_for_run_id(run_id, trace_dir=trace_dir)
        except ValidationError:
            skipped.error += 1
            continue

        if trace is None:
            skipped.no_trace += 1
            continue
        if trace.terminated_by == "budget_exceeded":
            skipped.budget_exceeded += 1
            continue
        if trace.terminated_by != TERMINAL_TOOL:
            # "error" terminations and any other non-terminal-tool exit
            # fold into the error bucket — RAGAS samples require the
            # propose_eval_case args to extract answer + retrieved IDs.
            skipped.error += 1
            continue

        proposal = _latest_proposal(trace)
        if proposal is None:
            skipped.error += 1
            continue
        args = proposal.args or {}
        proposed_failure_type = args.get("failure_type") or ""
        retrieved_context_ids = list(args.get("retrieved_context_ids") or [])

        try:
            answer = ragas_answer_from_trace(trace)
        except (KeyError, ValueError):
            skipped.error += 1
            continue

        produced_for_trace = 0
        saw_rag_call = False
        for tool_name, query, contexts in _iter_rag_tool_samples(trace):
            saw_rag_call = True
            if not contexts:
                skipped.no_context += 1
                continue
            samples.append(
                RagasSample(
                    run_id=trace.run_id,
                    question=query,
                    answer=answer,
                    contexts=contexts,
                    ground_truth_source=GROUND_TRUTH_SOURCE_NONE,
                    proposed_failure_type=proposed_failure_type,
                    retrieved_context_ids=retrieved_context_ids,
                    tool_name=tool_name,
                    tool_query=query,
                )
            )
            produced_for_trace += 1
        if produced_for_trace == 0 and not saw_rag_call:
            skipped.no_context += 1

    if limit is not None:
        samples = samples[:limit]
    return samples, skipped


def _exception_reason(prefix: str, exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")
    if not message:
        message = "<no message>"
    return f"{prefix}: {type(exc).__name__}: {message}"


def _ragas_import_failure() -> str | None:
    try:
        import ragas  # noqa: F401
        from ragas.metrics import faithfulness  # noqa: F401
    except (ImportError, ModuleNotFoundError) as exc:
        return _exception_reason("ragas import failed", exc)
    except Exception as exc:
        return _exception_reason("ragas import failed", exc)
    return None


def _run_real_ragas(samples: list[RagasSample]) -> dict[str, float]:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import faithfulness

    payload = {
        "user_input": [s.question for s in samples],
        "response": [s.answer for s in samples],
        "retrieved_contexts": [s.contexts for s in samples],
    }
    ds = Dataset.from_dict(payload)
    result = evaluate(ds, metrics=[faithfulness])
    df = result.to_pandas()
    means: dict[str, float] = {}
    for metric in ("faithfulness",):
        if metric in df.columns:
            means[metric] = float(df[metric].mean())
    return means


_PUNCTUATION_TABLE = str.maketrans("", "", string.punctuation)


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in text.lower().translate(_PUNCTUATION_TABLE).split()
        if token
    }


def faithfulness_stub(samples: list[RagasSample]) -> float:
    """Fraction of evidence claims with ≥50% token overlap to any context."""

    if not samples:
        return 0.0
    per_sample_scores: list[float] = []
    for sample in samples:
        # The evidence claims are embedded in the answer after the
        # actual_behavior preamble (separator: two newlines).
        if "\n\n" in sample.answer:
            _, _, claims_block = sample.answer.partition("\n\n")
            claims = [line for line in claims_block.splitlines() if line.strip()]
        else:
            claims = []
        if not claims:
            per_sample_scores.append(0.0)
            continue

        context_token_sets = [_tokenize(ctx) for ctx in sample.contexts]
        supported = 0
        for claim in claims:
            tokens = _tokenize(claim)
            if not tokens:
                continue
            best_overlap = 0.0
            for ctx_tokens in context_token_sets:
                if not ctx_tokens:
                    continue
                overlap = len(tokens & ctx_tokens) / len(tokens)
                if overlap > best_overlap:
                    best_overlap = overlap
            if best_overlap >= 0.5:
                supported += 1
        per_sample_scores.append(supported / max(1, len(claims)))
    return sum(per_sample_scores) / len(per_sample_scores)


def _resolve_ground_truth_source(samples: list[RagasSample]) -> str:
    sources = {s.ground_truth_source for s in samples}
    if not sources:
        return GROUND_TRUTH_SOURCE_NONE
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def build_report(
    samples: list[RagasSample],
    skipped: SkippedCounts,
    *,
    force_stub: bool = False,
) -> RagasReport:
    report = RagasReport(skipped=skipped)
    if not samples:
        report.ragas_mode = "stub"
        report.metric_means = {"faithfulness": 0.0}
        return report

    report.ground_truth_source = _resolve_ground_truth_source(samples)
    report.samples = [
        {
            "run_id": s.run_id,
            "question": s.question,
            "answer": s.answer,
            "contexts": s.contexts,
            "ground_truth_source": s.ground_truth_source,
            "proposed_failure_type": s.proposed_failure_type,
            "retrieved_context_ids": s.retrieved_context_ids,
            "tool_name": s.tool_name,
            "tool_query": s.tool_query,
        }
        for s in samples
    ]

    if force_stub:
        report.fallback_reason = "force_stub requested"
    elif not os.environ.get("OPENAI_API_KEY"):
        report.fallback_reason = "OPENAI_API_KEY is not set"
    else:
        import_failure = _ragas_import_failure()
        if import_failure is not None:
            report.fallback_reason = import_failure
        else:
            try:
                means = _run_real_ragas(samples)
                report.ragas_mode = "real"
                report.metric_means = means
                return report
            except Exception as exc:
                # Real path can fail because of model credentials, network,
                # quota, or package/API drift. Keep the report auditable while
                # still writing deterministic offline output.
                report.fallback_reason = _exception_reason("real ragas evaluation failed", exc)

    report.ragas_mode = "stub"
    report.metric_means = {
        "faithfulness": faithfulness_stub(samples),
    }
    return report


def write_report(report: RagasReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "ragas_report.json"
    md_path = output_dir / "ragas_report.md"

    json_payload = {
        "samples": report.samples,
        "metric_means": report.metric_means,
        "skipped": report.skipped.to_dict(),
        "ground_truth_source": report.ground_truth_source,
        "ragas_mode": report.ragas_mode,
        "fallback_reason": report.fallback_reason,
    }
    json_path.write_text(
        json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("# RAGAS Report")
    lines.append("")
    lines.append(f"- Sample count: {len(report.samples)}")
    lines.append(f"- Mode: `{report.ragas_mode}`")
    lines.append(f"- Ground truth source: `{report.ground_truth_source}`")
    if report.fallback_reason:
        lines.append(f"- Fallback reason: {report.fallback_reason}")
    lines.append("")
    lines.append("## Metric means")
    if report.metric_means:
        for metric, value in report.metric_means.items():
            label = f"{metric}_stub" if report.ragas_mode == "stub" else metric
            lines.append(f"- **{label}**: {value:.4f}")
        if report.ragas_mode == "stub":
            lines.append("")
            lines.append("`faithfulness_stub` is the fraction of evidence claims with at least 50% token overlap against any retrieved context.")
    else:
        lines.append(f"- (no metrics returned; sample count: {len(report.samples)})")
    lines.append("")
    lines.append("## Skipped traces")
    skipped = report.skipped.to_dict()
    for key in ("budget_exceeded", "error", "no_trace", "no_context"):
        lines.append(f"- {key}: {skipped[key]}")
    lines.append("")
    lines.append("## How this was generated")
    lines.append("")
    lines.append(
        f"`ragas_mode={report.ragas_mode}` — "
        + (
            "real `ragas` faithfulness evaluation over retrieved contexts."
            if report.ragas_mode == "real"
            else "pure-python stand-ins; no API key required."
        )
    )
    lines.append(
        f"`ground_truth_source={report.ground_truth_source}` — "
        + (
            "no artificial or self-generated ground truth is used; "
            "the report measures whether the final claims are supported "
            "by retrieved contexts."
            if report.ground_truth_source == GROUND_TRUTH_SOURCE_NONE
            else "labels are mixed; this is not expected for the Phase 8 A6 run."
        )
    )
    lines.append("")
    lines.append(
        "Trace source precedence (Phase 8 A6.1): explicit `--trace-dir` "
        "Phase 8 A2 dumps first at `<trace_dir>/<run_id>.json`; on miss, "
        "fall back to the SQLite `traces` table (`storage.load_trace`). "
        "The run-id discovery "
        "set is the union of SQLite-resident runs and `<trace_dir>/*.json` "
        "files."
    )
    lines.append(
        "Each RAGAS sample corresponds to one recorded `search_failure_memory` "
        "or `search_eval_cases` tool call: `question` is the tool query, "
        "`contexts` are that tool result's items, and `answer` is the final "
        "`propose_eval_case` actual_behavior plus evidence claims."
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual RAGAS eval over persisted traces.")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("TRAJECTA_DATA_DIR"),
        help="Override TRAJECTA_DATA_DIR (default: env var or <repo>/data).",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=None,
        help=(
            "Optional Phase 8 A2 trace-dump directory "
            "(eval/runs/<stamp>/traces/). When supplied, this source is "
            "preferred over SQLite per run_id so A6 binds to the selected "
            "agent_eval artefacts."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit the number of valid RAGAS samples after collection.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override report output dir (default: <repo>/eval).",
    )
    parser.add_argument(
        "--force-stub",
        action="store_true",
        help="Skip the real ragas path even if available.",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")

    # Resolve data dir.
    from backend.app.storage import REPO_ROOT

    data_root = Path(args.data_dir).resolve() if args.data_dir else (REPO_ROOT / "data").resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (REPO_ROOT / "eval").resolve()
    trace_dir = args.trace_dir.resolve() if args.trace_dir is not None else None

    print(f"RAGAS trace_dir={trace_dir if trace_dir is not None else '<sqlite-only>'}")
    print(f"RAGAS output_dir={output_dir}")
    if args.limit is not None:
        print(f"RAGAS limit={args.limit}")
    samples, skipped = collect_samples(data_root, trace_dir=trace_dir, limit=args.limit)
    print(f"RAGAS collected samples={len(samples)} skipped={skipped.to_dict()}")
    if args.force_stub:
        print("RAGAS mode request=stub (--force-stub)")
    elif os.environ.get("OPENAI_API_KEY"):
        print("RAGAS mode request=real (OPENAI_API_KEY is set)")
    else:
        print("RAGAS mode request=stub (OPENAI_API_KEY is not set)")
    print("RAGAS evaluate start")
    report = build_report(samples, skipped, force_stub=args.force_stub)
    print(f"RAGAS evaluate done mode={report.ragas_mode}")
    json_path, md_path = write_report(report, output_dir)

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(
        f"  samples={len(report.samples)} mode={report.ragas_mode} "
        f"means={report.metric_means} skipped={report.skipped.to_dict()}"
        + (f" trace_dir={trace_dir}" if trace_dir is not None else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
