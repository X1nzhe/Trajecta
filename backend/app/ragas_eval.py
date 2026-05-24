"""Manual RAGAS evaluation over cached agent traces.

Run as: ``python -m backend.app.ragas_eval``.

Reads every ``data/runs/*/last_trace.json``, keeps traces that terminated
via ``propose_eval_case``, and builds RAGAS samples without re-running
retrieval (the trace is the only evidence pool — see
``docs/testing.md`` and ``docs/eval_agent.md`` Observability).

Two execution modes:

- ``real``: uses the ``ragas`` package's ``faithfulness`` and
  ``context_precision`` metrics. Requires ``OPENAI_API_KEY`` and
  ``ragas`` to be importable.
- ``stub``: pure-Python stand-ins for both metrics that require neither
  a key nor a network. Selected automatically when ``ragas`` is missing
  or no key is present. Always writes the same two report files so the
  acceptance criterion (``eval/ragas_report.md`` exists) is met offline.

Stub heuristics — intentionally crude, see ``docs/testing.md``:

- ``faithfulness_stub``: fraction of evidence ``claim``s whose lower-cased
  tokens overlap ≥ 50% with any retrieved context string.
- ``context_precision_stub``: fraction of retrieved contexts whose
  ``case_id`` appears in the latest ``propose_eval_case`` call's
  ``retrieved_context_ids``.
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

from backend.app.schemas import AgentTrace, AgentTraceEvent


SEARCH_TOOL_NAMES = {"search_failure_memory", "search_eval_cases"}
TERMINAL_TOOL = "propose_eval_case"
RAGAS_QUESTION = "What failure pattern does this trajectory most closely match?"


@dataclass
class RagasSample:
    run_id: str
    question: str
    answer: str
    contexts: list[str]
    ground_truth: str
    ground_truth_source: str  # "fixture" | "self"
    proposed_failure_type: str
    retrieved_context_ids: list[str]


@dataclass
class SkippedCounts:
    budget_exceeded: int = 0
    error: int = 0
    no_trace: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "budget_exceeded": self.budget_exceeded,
            "error": self.error,
            "no_trace": self.no_trace,
        }


@dataclass
class RagasReport:
    samples: list[dict[str, Any]] = field(default_factory=list)
    metric_means: dict[str, float] = field(default_factory=dict)
    skipped: SkippedCounts = field(default_factory=SkippedCounts)
    ground_truth_source: str = "self"  # "fixture" | "self" | "mixed"
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


def _retrieved_contexts(trace: AgentTrace) -> list[str]:
    """Flatten every search_* tool_result event's items, across all turns."""

    out: list[str] = []
    for event in trace.events:
        if event.type != "tool_result" or event.name not in SEARCH_TOOL_NAMES:
            continue
        payload = event.result or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                out.append(_context_text_from_item(item))
    return out


def _retrieved_case_ids_from_contexts(trace: AgentTrace) -> list[str]:
    """case_id values surfaced by every search_* tool result, in arrival order."""

    out: list[str] = []
    for event in trace.events:
        if event.type != "tool_result" or event.name not in SEARCH_TOOL_NAMES:
            continue
        payload = event.result or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                case_id = item.get("case_id")
                if isinstance(case_id, str):
                    out.append(case_id)
    return out


def _ground_truth_from_disk(run_id: str, data_root: Path) -> str | None:
    path = data_root / "runs" / run_id / "ground_truth.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    value = payload.get("failure_type")
    return value if isinstance(value, str) and value else None


def collect_samples(
    data_root: Path,
) -> tuple[list[RagasSample], SkippedCounts]:
    samples: list[RagasSample] = []
    skipped = SkippedCounts()

    runs_root = data_root / "runs"
    if not runs_root.exists():
        return samples, skipped

    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        trace_path = run_dir / "last_trace.json"
        if not trace_path.exists():
            skipped.no_trace += 1
            continue
        try:
            trace = AgentTrace.model_validate_json(trace_path.read_text(encoding="utf-8"))
        except ValidationError:
            skipped.error += 1
            continue
        if trace.terminated_by == "budget_exceeded":
            skipped.budget_exceeded += 1
            continue
        if trace.terminated_by == "error":
            skipped.error += 1
            continue
        if trace.terminated_by != "propose_eval_case":
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

        gt = _ground_truth_from_disk(trace.run_id, data_root)
        if gt is None:
            ground_truth = proposed_failure_type
            ground_truth_source = "self"
        else:
            ground_truth = gt
            ground_truth_source = "fixture"

        samples.append(
            RagasSample(
                run_id=trace.run_id,
                question=RAGAS_QUESTION,
                answer=answer,
                contexts=_retrieved_contexts(trace),
                ground_truth=ground_truth,
                ground_truth_source=ground_truth_source,
                proposed_failure_type=proposed_failure_type,
                retrieved_context_ids=retrieved_context_ids,
            )
        )

    return samples, skipped


def _exception_reason(prefix: str, exc: Exception) -> str:
    message = str(exc).strip().replace("\n", " ")
    if not message:
        message = "<no message>"
    return f"{prefix}: {type(exc).__name__}: {message}"


def _ragas_import_failure() -> str | None:
    try:
        import ragas  # noqa: F401
        from ragas.metrics import context_precision, faithfulness  # noqa: F401
    except (ImportError, ModuleNotFoundError) as exc:
        return _exception_reason("ragas import failed", exc)
    except Exception as exc:
        return _exception_reason("ragas import failed", exc)
    return None


def _run_real_ragas(samples: list[RagasSample]) -> dict[str, float]:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import context_precision, faithfulness

    payload = {
        "question": [s.question for s in samples],
        "answer": [s.answer for s in samples],
        "contexts": [s.contexts for s in samples],
        "ground_truth": [s.ground_truth for s in samples],
    }
    ds = Dataset.from_dict(payload)
    result = evaluate(ds, metrics=[faithfulness, context_precision])
    df = result.to_pandas()
    means: dict[str, float] = {}
    for metric in ("faithfulness", "context_precision"):
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


def context_precision_stub(samples: list[RagasSample]) -> float:
    """Fraction of retrieved contexts whose case_id appears in
    EvalCase.retrieved_context_ids of the latest propose_eval_case call.
    """

    if not samples:
        return 0.0
    per_sample_scores: list[float] = []
    for sample in samples:
        if not sample.contexts:
            per_sample_scores.append(0.0)
            continue
        cited = set(sample.retrieved_context_ids)
        hits = 0
        for ctx in sample.contexts:
            # _context_text_from_item formats as "<case_id>: <summary> [<tags>]".
            case_id = ctx.split(":", 1)[0].strip()
            if case_id in cited:
                hits += 1
        per_sample_scores.append(hits / len(sample.contexts))
    return sum(per_sample_scores) / len(per_sample_scores)


def _resolve_ground_truth_source(samples: list[RagasSample]) -> str:
    sources = {s.ground_truth_source for s in samples}
    if not sources:
        return "self"
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
        report.metric_means = {"faithfulness": 0.0, "context_precision": 0.0}
        return report

    report.ground_truth_source = _resolve_ground_truth_source(samples)
    report.samples = [
        {
            "run_id": s.run_id,
            "question": s.question,
            "answer": s.answer,
            "contexts": s.contexts,
            "ground_truth": s.ground_truth,
            "ground_truth_source": s.ground_truth_source,
            "proposed_failure_type": s.proposed_failure_type,
            "retrieved_context_ids": s.retrieved_context_ids,
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
        "context_precision": context_precision_stub(samples),
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
            lines.append("`context_precision_stub` is the fraction of retrieved contexts whose case IDs were cited by the latest `propose_eval_case` call.")
    else:
        lines.append("- (no metrics — empty sample set)")
    lines.append("")
    lines.append("## Skipped traces")
    skipped = report.skipped.to_dict()
    for key in ("budget_exceeded", "error", "no_trace"):
        lines.append(f"- {key}: {skipped[key]}")
    lines.append("")
    lines.append("## How this was generated")
    lines.append("")
    lines.append(
        f"`ragas_mode={report.ragas_mode}` — "
        + (
            "real `ragas` evaluation with `faithfulness` and `context_precision`."
            if report.ragas_mode == "real"
            else "pure-python stand-ins; no API key required."
        )
    )
    lines.append(
        f"`ground_truth_source={report.ground_truth_source}` — "
        + (
            "labels read from `data/runs/<id>/ground_truth.json` fixtures."
            if report.ground_truth_source == "fixture"
            else "labels mirror the agent's own proposed failure_type (self-grading)."
            if report.ground_truth_source == "self"
            else "labels are a mix of disk fixtures and agent self-grading."
        )
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual RAGAS eval over cached traces.")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("TRAJECTA_DATA_DIR"),
        help="Override TRAJECTA_DATA_DIR (default: env var or <repo>/data).",
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

    # Resolve data dir.
    from backend.app.storage import REPO_ROOT

    data_root = Path(args.data_dir).resolve() if args.data_dir else (REPO_ROOT / "data").resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (REPO_ROOT / "eval").resolve()

    samples, skipped = collect_samples(data_root)
    report = build_report(samples, skipped, force_stub=args.force_stub)
    json_path, md_path = write_report(report, output_dir)

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(
        f"  samples={len(report.samples)} mode={report.ragas_mode} "
        f"means={report.metric_means} skipped={report.skipped.to_dict()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
