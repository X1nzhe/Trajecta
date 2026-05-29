"""Manual agent-quality evaluation over the human-triaged golden set.

Run as: ``python -m backend.app.agent_eval``.

This script is the **agent quality** counterpart to ``ragas_eval.py``. RAGAS
scores retrieval-grounded faithfulness over cached traces; this one scores
the agent's **diagnostic correctness** — does the proposed ``failure_type``
match the human label, does the proposed ``failure_step`` localize within
±2 steps, does the success/failure verdict match the label.

## Two execution modes

1. **Production eval** (default, ``python -m backend.app.agent_eval``):
   runs the real LLM agent end-to-end against every importable golden
   sample and writes ``eval/agent_report.{json,md}``. This is the eval
   you cite. Requires ``OPENAI_API_KEY`` + ``TRAJECTA_AGENT_MODEL`` to
   actually exercise a real LLM — without them ``_default_llm_client``
   silently falls back to ``OfflineAgentMock`` and the resulting numbers
   are meaningless (see below). Both vars are auto-loaded from
   ``.env`` at the repo root via ``python-dotenv`` (same pattern as
   ``backend/app/main.py`` and ``backend/tests/conftest.py``), so a
   plain ``python -m backend.app.agent_eval`` suffices once ``.env``
   is configured. Shell exports take precedence over the file.

2. **Pipeline smoke test** (``--mock``): forces the
   ``OfflineAgentMock`` backend. **Does not write the production report
   file** — only ``eval/_mock_smoke_test.json`` plus a stderr summary.
   The mock is a hardcoded 5-stage script, not a real agent; its
   "accuracy" numbers reflect the script's hardcoded output, not any
   diagnostic capability. Use this mode only to confirm wiring after
   refactors (CSV parsing, graph dispatch, grading logic, file I/O) —
   never as a quality claim.

## Statistical baselines (no agent run)

The report includes analytically-computed baselines from the label
distribution alone — majority-class and uniform-random. These exist
*only* to anchor the real LLM number: an LLM that scores below the
majority baseline is performing worse than "always guess the most
common failure_type". They do not involve any agent execution and are
identical across mock and real runs.

## Inputs / outputs

- ``data/triage_notes.csv`` — human triage with columns
  ``sample_id,category,outcome,failure_mode,failure_step,notes``. The
  ``failure_mode`` column may carry multiple labels separated by ``;`` (e.g.
  ``early_terminated;missed_constraint``); grading uses **multi-label OR
  policy (a)** — the agent is correct if its single proposed
  ``failure_type`` appears in the labeled set.
- ``eval/agent_report.json`` — full per-sample detail + aggregates + baselines.
- ``eval/agent_report.md`` — human-readable summary.
- ``eval/runs/<stamp>/agent_report.{json,md}`` — timestamped archive copy.
- ``eval/runs/<stamp>/traces/<run_id>.json`` — per-sample AgentTrace dumps
  (Phase 8 A2). Read by ``eval/judge.py`` (A3) and the real RAGAS run (A6);
  ``agent_report.json`` only carries source counts, not the full evidence
  chain, so the per-sample dumps are the canonical persistence path for
  downstream eval tools. The default location is shared with the archive
  copy so each timestamped eval run is self-contained. Suppress dumping
  with ``--trace-dir /dev/null`` is **not** supported — pass an explicit
  empty path only when you know the dir is writable. ``--mock`` mode
  defaults to no dump; pass ``--trace-dir <path>`` to force one.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import shutil
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Load .env from the repo root before any module reads os.environ. Mirrors the
# pattern in backend/app/main.py and backend/tests/conftest.py — every other
# entry point already does this; agent_eval shouldn't be the one that requires
# `OPENAI_API_KEY=... TRAJECTA_AGENT_MODEL=... python -m ...` on the CLI.
# Existing shell exports still take precedence (override=False is the default),
# so an explicit `export OPENAI_API_KEY=...` still wins over the file.
_REPO_ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv(_REPO_ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv is a hard dep
    pass

# Single-dataset model: the eval pipeline reads and writes the same
# ``data/`` tree as the rest of the app. Leakage is handled by per-tool
# field redaction (eval mode), not by maintaining a parallel snapshot.
# storage.data_dir() already defaults to ``<repo>/data`` when
# ``TRAJECTA_DATA_DIR`` is unset, so this module no longer overrides it.

from backend.app import eval_agent_graph, prompts, rag, storage
from backend.app.schemas import AgentTrace

TERMINAL_TOOL = "propose_eval_case"

# v1 failure-type vocabulary (see docs/contracts.md "v1 Failure Type
# Vocabulary"). Used as the denominator for the random baseline.
V1_FAILURE_VOCABULARY = (
    "early_terminated",
    "wrong_target",
    "wrong_result",
    "missed_constraint",
    "inefficient_search",
)


@dataclass
class GoldenSample:
    """One row of the human-triaged golden set."""

    run_id: str
    category: str
    outcome: str  # "success" | "failed"
    failure_types: list[str]  # multi-label parsed from CSV (empty for success)
    failure_step: int | None
    notes: str


@dataclass
class GradedSample:
    """A golden sample after agent inference and grading."""

    run_id: str
    category: str
    label_outcome: str
    label_failure_types: list[str]
    label_failure_step: int | None
    # Agent outputs (None when no terminal tool call was reached).
    proposed_failure_type: str | None
    proposed_failure_step: int | None
    proposed_is_success: bool
    terminated_by: str
    tool_call_count: int
    get_step_detail_count: int
    # Grading flags (None when not applicable, e.g. step localization for a
    # success sample).
    binary_verdict_correct: bool | None
    failure_type_correct: bool | None
    failure_step_within_2: bool | None
    # Cost / timing — sourced from trace + a wall-clock timer around
    # ``analyze_run``. ``input_tokens`` / ``output_tokens`` are the LLM
    # cumulative totals from ``AgentTrace``; ``vlm_*`` track VLM
    # consumption separately. ``latency_s`` is wall-clock from agent
    # start (post-storage-load) to terminal-tool return.
    input_tokens: int
    output_tokens: int
    vlm_input_tokens: int
    vlm_output_tokens: int
    prompt_version: str | None
    prompt_sha256: str | None
    latency_s: float
    # Trajectory size for coarse-to-fine ablation: total step count of
    # the run, so we can compare actual high-detail VLM calls against the
    # "naive full-detail" baseline of (step_count × 1500 tokens).
    step_count: int
    # Evidence quality — distribution of EvidenceItem.source values in the
    # final propose_eval_case args, plus the count of source=="unavailable"
    # items. ``evidence_total`` is the list length even when source breakdown
    # is empty (handles legacy args shapes).
    evidence_source_counts: dict[str, int]
    evidence_total: int
    evidence_unavailable: int


@dataclass
class SkippedCounts:
    not_importable: int = 0  # storage.load_run raised FileNotFoundError
    agent_error: int = 0  # analyze_run raised before terminating

    def to_dict(self) -> dict[str, int]:
        return {"not_importable": self.not_importable, "agent_error": self.agent_error}


@dataclass
class GoldenSetFilterSummary:
    original_n: int = 0
    evaluated_n: int = 0
    failure_memory_overlap_n: int = 0
    failure_memory_overlap_run_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_n": self.original_n,
            "evaluated_n": self.evaluated_n,
            "failure_memory_overlap_n": self.failure_memory_overlap_n,
            "failure_memory_overlap_run_ids": self.failure_memory_overlap_run_ids,
        }


@dataclass
class AgentEvalReport:
    samples: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    label_baselines: dict[str, Any] = field(default_factory=dict)
    per_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_quality: dict[str, Any] = field(default_factory=dict)
    cost_ablation: dict[str, Any] = field(default_factory=dict)
    golden_set_filter: GoldenSetFilterSummary = field(default_factory=GoldenSetFilterSummary)
    skipped: SkippedCounts = field(default_factory=SkippedCounts)
    grading_policy: str = "binary_primary_multi_label_or"
    primary_metric: str = "binary_verdict_accuracy"
    agent_mode: str = "auto"  # "auto" | "mock"
    notes: list[str] = field(default_factory=list)
    # Run identity / cost — populated by main() at end of run.
    started_at_utc: str = ""
    finished_at_utc: str = ""
    wall_clock_total_s: float = 0.0
    agent_model: str | None = None
    vlm_model: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    vlm_high_detail_prompt_version: str | None = None
    vlm_high_detail_prompt_sha256: str | None = None
    cost_usd: dict[str, Any] = field(default_factory=dict)


# Pricing source of truth lives at ``config/model_pricing.json`` (repo root)
# so backend and frontend stay in sync — see comments in that file for the
# schema. Loaded lazily and cached because the table is tiny and rarely
# changes; the cache also keeps it cheap to call _lookup_price_per_1m()
# from per-sample loops if that ever happens.
_PRICING_CONFIG_PATH = _REPO_ROOT / "config" / "model_pricing.json"
_pricing_entries_cache: list[dict[str, Any]] | None = None


def _load_pricing_entries() -> list[dict[str, Any]]:
    """Read pricing entries from config/model_pricing.json.

    Returns the raw entries list (already in first-match-wins order). On
    file-missing or malformed JSON, returns [] and the report carries a
    note via _compute_cost_usd — cost just falls through to null rather
    than crashing the eval.
    """
    global _pricing_entries_cache
    if _pricing_entries_cache is not None:
        return _pricing_entries_cache
    try:
        raw = json.loads(_PRICING_CONFIG_PATH.read_text(encoding="utf-8"))
        entries = raw.get("entries", [])
        if not isinstance(entries, list):
            entries = []
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    _pricing_entries_cache = entries
    return entries


def _lookup_price_per_1m(model: str | None) -> dict[str, float] | None:
    """Return {'input': $/1M, 'output': $/1M} for a known model, else None.

    Matching is case-insensitive regex, first hit wins (entry order in the
    JSON file decides specificity). Mirrors the frontend's resolution
    logic in EvalAgentPanel.tsx so a model id resolves to the same price
    on both sides.
    """
    if not model:
        return None
    import re
    for entry in _load_pricing_entries():
        pattern = entry.get("match")
        if not isinstance(pattern, str):
            continue
        try:
            if re.search(pattern, model, re.IGNORECASE):
                return {
                    "input": float(entry["input"]),
                    "output": float(entry["output"]),
                }
        except re.error:
            continue
    return None


def _compute_cost_usd(
    *,
    agent_input_tokens: int,
    agent_output_tokens: int,
    vlm_input_tokens: int,
    vlm_output_tokens: int,
    agent_model: str | None,
    vlm_model: str | None,
    overrides: dict[str, float | None],
) -> dict[str, Any]:
    """Estimate USD cost from token counts and resolved per-model prices.

    ``overrides`` keys: agent_input, agent_output, vlm_input, vlm_output.
    Any override (CLI flag) wins over the table lookup. If neither the
    override nor the table covers a model, that leg reports cost as null
    and the report carries a note explaining the gap.
    """
    notes: list[str] = []

    def resolve(model: str | None, override_in: float | None, override_out: float | None) -> tuple[float | None, float | None]:
        table = _lookup_price_per_1m(model)
        price_in = override_in if override_in is not None else (table["input"] if table else None)
        price_out = override_out if override_out is not None else (table["output"] if table else None)
        return price_in, price_out

    a_in_price, a_out_price = resolve(
        agent_model, overrides.get("agent_input"), overrides.get("agent_output")
    )
    v_in_price, v_out_price = resolve(
        vlm_model, overrides.get("vlm_input"), overrides.get("vlm_output")
    )

    if a_in_price is None or a_out_price is None:
        notes.append(
            f"Unknown agent model price for {agent_model!r}; agent cost reported as null."
            " Pass --agent-price-input / --agent-price-output to set USD per 1M tokens."
        )
    if v_in_price is None or v_out_price is None:
        notes.append(
            f"Unknown VLM model price for {vlm_model!r}; VLM cost reported as null."
            " Pass --vlm-price-input / --vlm-price-output to set USD per 1M tokens."
        )

    def usd(tokens: int, price_per_1m: float | None) -> float | None:
        if price_per_1m is None:
            return None
        return round(tokens * price_per_1m / 1_000_000.0, 6)

    agent_input_cost = usd(agent_input_tokens, a_in_price)
    agent_output_cost = usd(agent_output_tokens, a_out_price)
    vlm_input_cost = usd(vlm_input_tokens, v_in_price)
    vlm_output_cost = usd(vlm_output_tokens, v_out_price)

    parts = [c for c in (agent_input_cost, agent_output_cost, vlm_input_cost, vlm_output_cost) if c is not None]
    total = round(sum(parts), 6) if parts else None

    return {
        "agent_input_usd": agent_input_cost,
        "agent_output_usd": agent_output_cost,
        "vlm_input_usd": vlm_input_cost,
        "vlm_output_usd": vlm_output_cost,
        "total_usd": total,
        "prices_per_1m_tokens": {
            "agent_input": a_in_price,
            "agent_output": a_out_price,
            "vlm_input": v_in_price,
            "vlm_output": v_out_price,
        },
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# CSV loading


def _parse_failure_modes(raw: str) -> list[str]:
    """Split a multi-label failure_mode cell on ``;`` and strip whitespace.

    Defensive against stray spaces (e.g. ``inefficient_search ``) — every
    label is ``.strip()``-ed. Empty cells return ``[]``.
    """
    if not raw or not raw.strip():
        return []
    parts = [piece.strip() for piece in raw.split(";")]
    return [p for p in parts if p]


def _parse_step(raw: str) -> int | None:
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def load_golden_set(csv_path: Path) -> list[GoldenSample]:
    samples: list[GoldenSample] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples.append(
                GoldenSample(
                    run_id=(row.get("sample_id") or "").strip(),
                    category=(row.get("category") or "").strip().lower(),
                    outcome=(row.get("outcome") or "").strip().lower(),
                    failure_types=_parse_failure_modes(row.get("failure_mode") or ""),
                    failure_step=_parse_step(row.get("failure_step") or ""),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return samples


def _failure_memory_source_run_ids() -> set[str]:
    """Return run_ids used as curated failure-memory sources."""

    return {
        case.source_run_id
        for case in storage.load_failure_memory()
        if case.source_run_id
    }


def filter_golden_set_for_failure_memory_overlap(
    golden: list[GoldenSample],
    failure_memory_source_run_ids: set[str] | None = None,
) -> tuple[list[GoldenSample], GoldenSetFilterSummary]:
    """Drop golden rows whose run_id appears in failure_memory source_run_id."""

    source_run_ids = (
        _failure_memory_source_run_ids()
        if failure_memory_source_run_ids is None
        else failure_memory_source_run_ids
    )
    filtered = [sample for sample in golden if sample.run_id not in source_run_ids]
    excluded = [sample.run_id for sample in golden if sample.run_id in source_run_ids]
    return filtered, GoldenSetFilterSummary(
        original_n=len(golden),
        evaluated_n=len(filtered),
        failure_memory_overlap_n=len(excluded),
        failure_memory_overlap_run_ids=sorted(set(excluded)),
    )


# ---------------------------------------------------------------------------
# Trace inspection


def _latest_propose_args(trace: AgentTrace) -> dict[str, Any] | None:
    for event in reversed(trace.events):
        if event.type == "tool_call" and event.name == TERMINAL_TOOL:
            return event.args or {}
    return None


def _get_step_detail_count(trace: AgentTrace) -> int:
    return sum(
        1
        for ev in trace.events
        if ev.type == "tool_call" and ev.name == "get_step_detail"
    )


def _evidence_breakdown(args: dict[str, Any] | None) -> tuple[dict[str, int], int, int]:
    """Extract source distribution + unavailable count from a propose_eval_case
    args' ``evidence`` field. Tolerates both list-of-EvidenceItem and
    list-of-dict shapes (the agent emits dicts; the trace stores dicts)."""
    if not args:
        return {}, 0, 0
    evidence = args.get("evidence")
    if not isinstance(evidence, list):
        return {}, 0, 0
    counts: dict[str, int] = {}
    unavailable = 0
    for item in evidence:
        source = None
        if isinstance(item, dict):
            source = item.get("source")
        else:
            source = getattr(item, "source", None)
        if not isinstance(source, str):
            continue
        counts[source] = counts.get(source, 0) + 1
        if source == "unavailable":
            unavailable += 1
    return counts, len(evidence), unavailable


# ---------------------------------------------------------------------------
# Grading


def _grade(
    sample: GoldenSample,
    trace: AgentTrace,
    *,
    latency_s: float,
    step_count: int,
) -> GradedSample:
    """Apply multi-label OR grading policy (a) to one agent trace."""
    args = _latest_propose_args(trace)
    evidence_counts, evidence_total, evidence_unavailable = _evidence_breakdown(args)
    proposed_type: str | None = None
    proposed_step: int | None = None
    proposed_is_success: bool = trace.terminated_by == TERMINAL_TOOL and args is not None
    if args is not None:
        raw_type = args.get("failure_type")
        if isinstance(raw_type, str) and raw_type.strip():
            proposed_type = raw_type.strip()
        raw_step = args.get("failure_step")
        if isinstance(raw_step, int):
            proposed_step = raw_step
        elif isinstance(raw_step, str) and raw_step.strip():
            try:
                proposed_step = int(raw_step.strip())
            except ValueError:
                proposed_step = None
        # Success verdict per docs/eval_agent.md: all five failure fields
        # omitted (failure_type included), only evidence + retrieved_context_ids.
        proposed_is_success = proposed_type is None
    else:
        # Trace did not terminate via propose_eval_case → cannot make any verdict.
        proposed_is_success = False

    # Binary verdict grading.
    label_is_success = sample.outcome == "success"
    if args is None:
        binary_correct: bool | None = False  # no verdict = wrong by default
    else:
        binary_correct = proposed_is_success == label_is_success

    # failure_type accuracy — multi-label OR policy.
    # Only applies when label is "failed". For success samples, this column is N/A.
    failure_type_correct: bool | None
    if label_is_success:
        failure_type_correct = None
    elif proposed_type is None:
        failure_type_correct = False
    else:
        failure_type_correct = proposed_type in set(sample.failure_types)

    # failure_step localization — only for labeled failed samples with a
    # ground-truth step.
    failure_step_within_2: bool | None
    if label_is_success or sample.failure_step is None:
        failure_step_within_2 = None
    elif proposed_step is None:
        failure_step_within_2 = False
    else:
        failure_step_within_2 = abs(proposed_step - sample.failure_step) <= 2

    return GradedSample(
        run_id=sample.run_id,
        category=sample.category,
        label_outcome=sample.outcome,
        label_failure_types=sample.failure_types,
        label_failure_step=sample.failure_step,
        proposed_failure_type=proposed_type,
        proposed_failure_step=proposed_step,
        proposed_is_success=proposed_is_success,
        terminated_by=trace.terminated_by,
        tool_call_count=trace.tool_call_count,
        get_step_detail_count=_get_step_detail_count(trace),
        binary_verdict_correct=binary_correct,
        failure_type_correct=failure_type_correct,
        failure_step_within_2=failure_step_within_2,
        input_tokens=getattr(trace, "input_tokens", 0) or 0,
        output_tokens=getattr(trace, "output_tokens", 0) or 0,
        vlm_input_tokens=getattr(trace, "vlm_input_tokens", 0) or 0,
        vlm_output_tokens=getattr(trace, "vlm_output_tokens", 0) or 0,
        prompt_version=getattr(trace, "prompt_version", None),
        prompt_sha256=getattr(trace, "prompt_sha256", None),
        latency_s=latency_s,
        step_count=step_count,
        evidence_source_counts=evidence_counts,
        evidence_total=evidence_total,
        evidence_unavailable=evidence_unavailable,
    )


# ---------------------------------------------------------------------------
# Driver


@contextlib.contextmanager
def _forced_mock_env():
    """Temporarily clear the real-LLM env vars so ``_default_llm_client`` falls
    through to ``OfflineAgentMock``.

    Why not pass ``llm_client=OfflineAgentMock(state)`` directly? The
    ``analyze_run`` signature accepts ``llm_client`` but the mock needs the
    LangGraph ``state`` dict (which only exists inside ``_default_llm_client``)
    to know the ``run_id`` and ``trajectory_digest``. The cleanest way to force
    the mock from outside is to make the env look like the no-credentials case
    that the production builder already handles.
    """
    saved: dict[str, str] = {}
    for key in ("OPENAI_API_KEY", "TRAJECTA_AGENT_MODEL"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    try:
        yield
    finally:
        for key, value in saved.items():
            os.environ[key] = value


def _run_agent(run_id: str, *, force_mock: bool) -> AgentTrace:
    if force_mock:
        with _forced_mock_env():
            result = eval_agent_graph.analyze_run(run_id, persist=False)
    else:
        result = eval_agent_graph.analyze_run(run_id, persist=False)
    return result.trace


def _dump_trace(trace: AgentTrace, trace_dir: Path, run_id: str) -> Path | None:
    """Persist one AgentTrace as ``{trace_dir}/{run_id}.json``.

    Phase 8 A2 — the judge (A3) and the real RAGAS run (A6) both need the
    full evidence + tool-result chain, which the aggregate
    ``agent_report.json`` does not preserve (it only keeps source-counts
    and verdict-shape fields). Per-sample dumps make that chain
    available off-disk for downstream tools.

    Returns the written path, or ``None`` when dumping was attempted but
    failed (disk-full, permission error, etc.). Failures are logged to
    stderr and **do not** propagate — a flaky filesystem must not lose
    an otherwise-successful grading run.
    """
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        out_path = trace_dir / f"{run_id}.json"
        out_path.write_text(
            trace.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return out_path
    except OSError as exc:  # pragma: no cover - defensive
        print(
            f"  ! failed to dump trace for {run_id[:12]}…: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None


def collect_graded_samples(
    golden: list[GoldenSample],
    *,
    force_mock: bool,
    limit: int | None = None,
    trace_dir: Path | None = None,
) -> tuple[list[GradedSample], SkippedCounts]:
    """Run the agent against every importable sample and grade each result.

    Emits per-sample progress to stderr — important because real-LLM runs
    can take 30s–3min each and a silent 20-minute wait gives no way to
    distinguish "still running" from "deadlocked". The format
    ``[i/N] runid_prefix… status (timing/details)`` lets you visually scan
    for slowdowns or pattern shifts (e.g. all booking runs taking 3× longer).

    When ``trace_dir`` is set, each agent run's full ``AgentTrace`` is
    serialised to ``{trace_dir}/{run_id}.json`` via :func:`_dump_trace`.
    The dump is the persistence path the judge (A3) and the real RAGAS
    run (A6) read from. Dump failures are logged but do not abort the
    grading run.
    """
    graded: list[GradedSample] = []
    skipped = SkippedCounts()
    subset = golden if limit is None else golden[:limit]
    total = len(subset)
    overall_start = time.perf_counter()
    for i, sample in enumerate(subset, start=1):
        prefix = f"[{i:2d}/{total}] {sample.run_id[:12]}…"
        try:
            run = storage.load_run(sample.run_id)
        except FileNotFoundError:
            skipped.not_importable += 1
            print(f"{prefix} skipped: not importable in storage", file=sys.stderr)
            continue
        step_count = len(run.steps)
        print(
            f"{prefix} starting  (category={sample.category}, "
            f"label={sample.outcome}, step_count={step_count})",
            file=sys.stderr,
            flush=True,
        )
        start = time.perf_counter()
        try:
            trace = _run_agent(sample.run_id, force_mock=force_mock)
        except Exception as exc:  # pragma: no cover - defensive
            elapsed = time.perf_counter() - start
            skipped.agent_error += 1
            print(
                f"{prefix} ERROR after {elapsed:.1f}s: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        latency_s = time.perf_counter() - start
        if trace_dir is not None:
            _dump_trace(trace, trace_dir, sample.run_id)
        graded_sample = _grade(
            sample, trace, latency_s=latency_s, step_count=step_count
        )
        graded.append(graded_sample)
        verdict = graded_sample.proposed_failure_type or "success"
        print(
            f"{prefix} done in {latency_s:5.1f}s  "
            f"(tools={graded_sample.tool_call_count}, "
            f"get_step_detail={graded_sample.get_step_detail_count}, "
            f"verdict={verdict}, terminated_by={trace.terminated_by})",
            file=sys.stderr,
            flush=True,
        )

    overall_elapsed = time.perf_counter() - overall_start
    print(
        f"\nFinished {total} samples in {overall_elapsed:.1f}s "
        f"({overall_elapsed / 60:.1f} min)",
        file=sys.stderr,
    )
    return graded, skipped


# ---------------------------------------------------------------------------
# Metrics


def _mean(values: list[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


# Approximate cost of one high-detail VLM call, matching the figure used
# throughout docs/eval_agent.md "Cost Strategy" table. Used to compute the
# naive-vs-coarse-to-fine savings claim.
HIGH_DETAIL_VLM_TOKENS_PER_IMAGE = 1500
LOW_DETAIL_VLM_TOKENS_PER_IMAGE = 85


def compute_label_baselines(golden: list[GoldenSample]) -> dict[str, Any]:
    """Analytical baselines from the label distribution alone.

    Returns both binary baselines (success vs failed — the primary signal)
    and 5-class failure_type baselines (advisory only, see report caveats).
    Does NOT run the agent. These exist to anchor the real LLM accuracy:
    any agent below the majority baseline is performing worse than
    "always guess the dominant class".
    """
    # --- Binary baselines (primary metric) -------------------------------
    # Over the full graded set, regardless of failure_types presence.
    total_n = len(golden)
    success_n = sum(1 for s in golden if s.outcome == "success")
    failed_n = sum(1 for s in golden if s.outcome == "failed")
    if total_n == 0:
        binary_baselines: dict[str, Any] = {
            "binary_majority_class": None,
            "binary_majority_baseline_accuracy": 0.0,
            "binary_random_baseline_accuracy": 0.0,
            "n_binary_samples": 0,
            "binary_success_n": 0,
            "binary_failed_n": 0,
        }
    else:
        binary_majority_class = "success" if success_n >= failed_n else "failed"
        binary_majority_n = max(success_n, failed_n)
        binary_baselines = {
            "binary_majority_class": binary_majority_class,
            "binary_majority_baseline_accuracy": binary_majority_n / total_n,
            "binary_random_baseline_accuracy": 0.5,  # uniform over 2 classes
            "n_binary_samples": total_n,
            "binary_success_n": success_n,
            "binary_failed_n": failed_n,
        }

    # --- failure_type baselines (advisory) -------------------------------
    failed = [s for s in golden if s.outcome == "failed" and s.failure_types]
    if not failed:
        ftype_baselines: dict[str, Any] = {
            "majority_class": None,
            "majority_baseline_accuracy": 0.0,
            "random_baseline_expected_accuracy": 0.0,
            "n_failed_samples": 0,
            "vocabulary_size": len(V1_FAILURE_VOCABULARY),
        }
        return {**binary_baselines, **ftype_baselines}

    # Per-sample contribution to majority/random — multi-label OR policy:
    # a sample contributes a hit if the predicted class is in its label set.
    label_counts: Counter[str] = Counter()
    for s in failed:
        for t in s.failure_types:
            label_counts[t] += 1
    majority_class = label_counts.most_common(1)[0][0]
    majority_hits = sum(
        1 for s in failed if majority_class in set(s.failure_types)
    )
    majority_acc = majority_hits / len(failed)

    # Uniform random over V1 vocabulary. Per-sample expected hit =
    # |unique label set| / vocabulary_size. Mean across samples.
    vocab = len(V1_FAILURE_VOCABULARY)
    random_expected_acc = statistics.fmean(
        len(set(s.failure_types)) / vocab for s in failed
    )

    ftype_baselines = {
        "majority_class": majority_class,
        "majority_baseline_accuracy": majority_acc,
        "random_baseline_expected_accuracy": random_expected_acc,
        "n_failed_samples": len(failed),
        "vocabulary_size": vocab,
        "label_distribution": dict(label_counts),
    }
    return {**binary_baselines, **ftype_baselines}


def compute_per_category(graded: list[GradedSample]) -> dict[str, dict[str, Any]]:
    """Break down core metrics by category (=site). Reveals site-specific
    over- or under-performance, e.g. agent good on github / blind on apple."""
    by_cat: dict[str, list[GradedSample]] = {}
    for s in graded:
        by_cat.setdefault(s.category or "(unknown)", []).append(s)

    out: dict[str, dict[str, Any]] = {}
    for cat, items in sorted(by_cat.items()):
        binary_n = sum(1 for s in items if s.binary_verdict_correct is not None)
        binary_ok = sum(1 for s in items if s.binary_verdict_correct is True)
        ftype_n = sum(1 for s in items if s.failure_type_correct is not None)
        ftype_ok = sum(1 for s in items if s.failure_type_correct is True)
        out[cat] = {
            "n": len(items),
            "binary_accuracy": _ratio(binary_ok, binary_n),
            "failure_type_accuracy": _ratio(ftype_ok, ftype_n),
            "mean_tool_call_count": _mean([s.tool_call_count for s in items]),
            "mean_latency_s": _mean([s.latency_s for s in items]),
        }
    return out


def compute_evidence_quality(graded: list[GradedSample]) -> dict[str, Any]:
    """Aggregate EvidenceItem source distribution across all graded runs.

    Tells you whether the agent is grounding claims in high-detail
    inspection (good), low-detail digests (weak), retrieved memory
    (good if relevant), or marking gaps as ``unavailable`` (honest about
    missing data). A trace with lots of ``trajectory_digest``-only
    evidence and few ``step_detail_high`` claims is suspicious.
    """
    pooled: Counter[str] = Counter()
    total_items = 0
    total_unavailable = 0
    samples_with_unavailable = 0
    for s in graded:
        for source, count in s.evidence_source_counts.items():
            pooled[source] += count
        total_items += s.evidence_total
        total_unavailable += s.evidence_unavailable
        if s.evidence_unavailable > 0:
            samples_with_unavailable += 1
    return {
        "source_distribution": dict(pooled),
        "total_evidence_items": total_items,
        "unavailable_items": total_unavailable,
        "samples_with_unavailable": samples_with_unavailable,
    }


def compute_cost_ablation(graded: list[GradedSample]) -> dict[str, Any]:
    """Quantify the coarse-to-fine VLM savings against a naive baseline.

    Naive baseline: every step gets a high-detail VLM pass (the strawman
    the README cost demo argues against). Actual: per-step low-detail
    preprocessing + on-demand high-detail via ``get_step_detail``.

    Per-step low-detail summary cost is **baked into preprocessing**
    and is shared by the naive comparator too (both pay for orientation
    once per step). So the meaningful diff is high-detail calls only.
    Reports both absolute tokens and a ratio.
    """
    if not graded:
        return {
            "actual_high_detail_tokens": 0,
            "naive_high_detail_tokens": 0,
            "savings_ratio": 0.0,
            "mean_high_detail_calls_per_run": 0.0,
            "mean_step_count": 0.0,
        }
    actual_high = sum(
        s.get_step_detail_count * HIGH_DETAIL_VLM_TOKENS_PER_IMAGE for s in graded
    )
    naive_high = sum(
        s.step_count * HIGH_DETAIL_VLM_TOKENS_PER_IMAGE for s in graded
    )
    savings_ratio = 1.0 - (actual_high / naive_high) if naive_high else 0.0
    return {
        "actual_high_detail_tokens": actual_high,
        "naive_high_detail_tokens": naive_high,
        "savings_ratio": savings_ratio,
        "mean_high_detail_calls_per_run": _mean(
            [float(s.get_step_detail_count) for s in graded]
        ),
        "mean_step_count": _mean([float(s.step_count) for s in graded]),
    }


def compute_metrics(graded: list[GradedSample]) -> dict[str, Any]:
    """Aggregate top-1 metrics across all graded samples.

    Per-class precision/recall is intentionally omitted: with N=1–4 per class
    in the v1 golden set, per-class numbers are noisier than the aggregate.
    See ``docs/testing.md`` "Per-class N" note.
    """
    binary_total = sum(1 for s in graded if s.binary_verdict_correct is not None)
    binary_correct = sum(1 for s in graded if s.binary_verdict_correct is True)

    ftype_total = sum(1 for s in graded if s.failure_type_correct is not None)
    ftype_correct = sum(1 for s in graded if s.failure_type_correct is True)

    fstep_total = sum(1 for s in graded if s.failure_step_within_2 is not None)
    fstep_correct = sum(1 for s in graded if s.failure_step_within_2 is True)

    success_subset = [s for s in graded if s.label_outcome == "success"]
    success_total = len(success_subset)
    success_correct = sum(1 for s in success_subset if s.binary_verdict_correct is True)

    failed_subset = [s for s in graded if s.label_outcome == "failed"]
    failed_total = len(failed_subset)
    failed_correct = sum(1 for s in failed_subset if s.binary_verdict_correct is True)

    terminated_by_dist: dict[str, int] = {}
    for s in graded:
        terminated_by_dist[s.terminated_by] = (
            terminated_by_dist.get(s.terminated_by, 0) + 1
        )

    total_input_tokens = sum(s.input_tokens for s in graded)
    total_output_tokens = sum(s.output_tokens for s in graded)
    total_vlm_input_tokens = sum(s.vlm_input_tokens for s in graded)
    total_vlm_output_tokens = sum(s.vlm_output_tokens for s in graded)

    return {
        "binary_verdict_accuracy": _ratio(binary_correct, binary_total),
        "binary_verdict_n": binary_total,
        "success_verdict_recall": _ratio(success_correct, success_total),
        "success_verdict_n": success_total,
        "failure_verdict_recall": _ratio(failed_correct, failed_total),
        "failure_verdict_n": failed_total,
        "failure_type_top1_accuracy": _ratio(ftype_correct, ftype_total),
        "failure_type_n": ftype_total,
        "failure_step_localization_within_2": _ratio(fstep_correct, fstep_total),
        "failure_step_n": fstep_total,
        "mean_tool_call_count": _mean([s.tool_call_count for s in graded]),
        "mean_get_step_detail_count": _mean(
            [float(s.get_step_detail_count) for s in graded]
        ),
        "terminated_by_distribution": terminated_by_dist,
        "graded_n": len(graded),
        "mean_latency_s": _mean([s.latency_s for s in graded]),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "mean_input_tokens_per_run": _mean([float(s.input_tokens) for s in graded]),
        "mean_output_tokens_per_run": _mean([float(s.output_tokens) for s in graded]),
        "total_vlm_input_tokens": total_vlm_input_tokens,
        "total_vlm_output_tokens": total_vlm_output_tokens,
    }


def _ratio(num: int, den: int) -> float:
    return float(num) / den if den else 0.0


# ---------------------------------------------------------------------------
# Report I/O


def build_report(
    graded: list[GradedSample],
    skipped: SkippedCounts,
    *,
    agent_mode: str,
    label_baselines: dict[str, Any],
    golden_set_filter: GoldenSetFilterSummary | None = None,
) -> AgentEvalReport:
    report = AgentEvalReport(agent_mode=agent_mode)
    report.samples = [
        {
            "run_id": s.run_id,
            "category": s.category,
            "label_outcome": s.label_outcome,
            "label_failure_types": s.label_failure_types,
            "label_failure_step": s.label_failure_step,
            "proposed_failure_type": s.proposed_failure_type,
            "proposed_failure_step": s.proposed_failure_step,
            "proposed_is_success": s.proposed_is_success,
            "terminated_by": s.terminated_by,
            "tool_call_count": s.tool_call_count,
            "get_step_detail_count": s.get_step_detail_count,
            "binary_verdict_correct": s.binary_verdict_correct,
            "failure_type_correct": s.failure_type_correct,
            "failure_step_within_2": s.failure_step_within_2,
            "input_tokens": s.input_tokens,
            "output_tokens": s.output_tokens,
            "vlm_input_tokens": s.vlm_input_tokens,
            "vlm_output_tokens": s.vlm_output_tokens,
            "prompt_version": s.prompt_version,
            "prompt_sha256": s.prompt_sha256,
            "latency_s": round(s.latency_s, 3),
            "step_count": s.step_count,
            "evidence_source_counts": s.evidence_source_counts,
            "evidence_total": s.evidence_total,
            "evidence_unavailable": s.evidence_unavailable,
        }
        for s in graded
    ]
    report.metrics = compute_metrics(graded)
    report.label_baselines = label_baselines
    report.per_category = compute_per_category(graded)
    report.evidence_quality = compute_evidence_quality(graded)
    report.cost_ablation = compute_cost_ablation(graded)
    if golden_set_filter is not None:
        report.golden_set_filter = golden_set_filter
    report.skipped = skipped
    if report.metrics.get("graded_n", 0) == 0:
        report.notes.append(
            "No samples were gradeable. Verify the golden set CSV path and that the"
            " labeled runs are imported into storage."
        )
    return report


def _format_pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def write_report(
    report: AgentEvalReport, output_dir: Path
) -> tuple[Path, Path]:
    """Write the full production eval report (JSON + Markdown).

    Always emits both files. Used for the real-LLM path. For ``--mock``
    runs, use ``write_mock_smoke_test`` instead — mock numbers are not a
    quality claim and should not collide with the production artifact name.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "agent_report.json"
    md_path = output_dir / "agent_report.md"

    json_path.write_text(
        json.dumps(
            {
                "started_at_utc": report.started_at_utc,
                "finished_at_utc": report.finished_at_utc,
                "wall_clock_total_s": report.wall_clock_total_s,
                "agent_mode": report.agent_mode,
                "agent_model": report.agent_model,
                "vlm_model": report.vlm_model,
                "prompt_version": report.prompt_version,
                "prompt_sha256": report.prompt_sha256,
                "vlm_high_detail_prompt_version": report.vlm_high_detail_prompt_version,
                "vlm_high_detail_prompt_sha256": report.vlm_high_detail_prompt_sha256,
                "grading_policy": report.grading_policy,
                "primary_metric": report.primary_metric,
                "cost_usd": report.cost_usd,
                "metrics": report.metrics,
                "label_baselines": report.label_baselines,
                "per_category": report.per_category,
                "evidence_quality": report.evidence_quality,
                "cost_ablation": report.cost_ablation,
                "golden_set_filter": report.golden_set_filter.to_dict(),
                "skipped": report.skipped.to_dict(),
                "samples": report.samples,
                "notes": report.notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    m = report.metrics
    bl = report.label_baselines
    ev = report.evidence_quality
    ca = report.cost_ablation
    lines: list[str] = []
    lines.append("# Agent Quality Eval Report")
    lines.append("")
    if report.started_at_utc:
        lines.append(f"- Started: `{report.started_at_utc}`")
    if report.finished_at_utc:
        lines.append(f"- Finished: `{report.finished_at_utc}`")
    if report.wall_clock_total_s:
        lines.append(f"- Wall-clock total: **{report.wall_clock_total_s:.1f}s**")
    lines.append(f"- Agent mode: `{report.agent_mode}`")
    if report.agent_model:
        lines.append(f"- Agent model: `{report.agent_model}`")
    if report.vlm_model:
        lines.append(f"- VLM model: `{report.vlm_model}`")
    if report.prompt_version:
        lines.append(f"- Prompt version: `{report.prompt_version}`")
    if report.prompt_sha256:
        lines.append(f"- Prompt SHA-256: `{report.prompt_sha256}`")
    if report.vlm_high_detail_prompt_version:
        lines.append(f"- VLM high-detail prompt version: `{report.vlm_high_detail_prompt_version}`")
    if report.vlm_high_detail_prompt_sha256:
        lines.append(f"- VLM high-detail prompt SHA-256: `{report.vlm_high_detail_prompt_sha256}`")
    lines.append(f"- Grading policy: `{report.grading_policy}`")
    lines.append(f"- Graded samples: **{m.get('graded_n', 0)}**")
    gf = report.golden_set_filter
    lines.append(
        f"- Golden set filter: original={gf.original_n}, "
        f"excluded_failure_memory_overlap={gf.failure_memory_overlap_n}, "
        f"evaluated={gf.evaluated_n}"
    )
    lines.append(
        f"- Skipped: not_importable={report.skipped.not_importable},"
        f" agent_error={report.skipped.agent_error}"
    )
    total_cost = report.cost_usd.get("total_usd") if report.cost_usd else None
    if total_cost is not None:
        lines.append(f"- **Total cost: ${total_cost:.4f} USD**")
    lines.append("")

    # ---- Headline (primary metric): binary verdict accuracy ----
    # Demoted from the previous 5-class failure_type headline because the
    # taxonomy has overlapping definitions + high inter-annotator noise.
    # See ## Caveats below for the rationale.
    lines.append("## Binary verdict accuracy vs. baselines")
    lines.append("")
    lines.append(
        "**Primary metric.** Does the agent correctly identify whether the"
        " trajectory succeeded or failed at its task? Coarser than"
        " failure_type classification but far more reliable —"
        " human inter-annotator agreement is high on this axis."
    )
    lines.append("")
    lines.append("| Method | Binary accuracy | N | Notes |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| **Random baseline** (50/50, analytical) "
        f"| {_format_pct(bl.get('binary_random_baseline_accuracy', 0.5))} "
        f"| {bl.get('n_binary_samples', 0)} | Uniform over 2 classes |"
    )
    lines.append(
        f"| **Majority baseline** (always predict `{bl.get('binary_majority_class') or '—'}`, analytical) "
        f"| {_format_pct(bl.get('binary_majority_baseline_accuracy', 0.0))} "
        f"| {bl.get('n_binary_samples', 0)} "
        f"| success={bl.get('binary_success_n', 0)}, failed={bl.get('binary_failed_n', 0)} |"
    )
    lines.append(
        f"| **Agent** (`{report.agent_mode}`) "
        f"| {_format_pct(m.get('binary_verdict_accuracy', 0.0))} "
        f"| {m.get('binary_verdict_n', 0)} | task-completion verdict |"
    )
    lines.append("")

    lines.append("## Recall breakdown by gold class")
    lines.append("")
    lines.append("| Metric | Value | N |")
    lines.append("|---|---|---|")
    lines.append(
        f"| Success-verdict recall | {_format_pct(m.get('success_verdict_recall', 0.0))} | {m.get('success_verdict_n', 0)} |"
    )
    lines.append(
        f"| Failure-verdict recall | {_format_pct(m.get('failure_verdict_recall', 0.0))} | {m.get('failure_verdict_n', 0)} |"
    )
    lines.append("")

    lines.append("## Cost & latency")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Mean wall-clock latency / run | {m.get('mean_latency_s', 0.0):.2f}s |")
    lines.append(f"| Mean tool_call_count / run | {m.get('mean_tool_call_count', 0.0):.2f} |")
    lines.append(f"| Mean `get_step_detail` / run | {m.get('mean_get_step_detail_count', 0.0):.2f} |")
    lines.append(f"| Mean LLM input tokens / run | {m.get('mean_input_tokens_per_run', 0.0):.0f} |")
    lines.append(f"| Mean LLM output tokens / run | {m.get('mean_output_tokens_per_run', 0.0):.0f} |")
    lines.append(f"| Total LLM input tokens | {m.get('total_input_tokens', 0)} |")
    lines.append(f"| Total LLM output tokens | {m.get('total_output_tokens', 0)} |")
    lines.append(f"| Total VLM input tokens | {m.get('total_vlm_input_tokens', 0)} |")
    lines.append(f"| Total VLM output tokens | {m.get('total_vlm_output_tokens', 0)} |")
    if report.cost_usd:
        c = report.cost_usd
        def _fmt_usd(v: Any) -> str:
            return f"${v:.4f}" if isinstance(v, (int, float)) else "—"
        lines.append(f"| Agent input cost | {_fmt_usd(c.get('agent_input_usd'))} |")
        lines.append(f"| Agent output cost | {_fmt_usd(c.get('agent_output_usd'))} |")
        lines.append(f"| VLM input cost | {_fmt_usd(c.get('vlm_input_usd'))} |")
        lines.append(f"| VLM output cost | {_fmt_usd(c.get('vlm_output_usd'))} |")
        lines.append(f"| **Total cost** | **{_fmt_usd(c.get('total_usd'))}** |")
        prices = c.get("prices_per_1m_tokens", {})
        if any(v is not None for v in prices.values()):
            lines.append("")
            lines.append("Prices used (USD per 1M tokens):")
            lines.append("")
            for label, key in (
                ("agent input", "agent_input"),
                ("agent output", "agent_output"),
                ("VLM input", "vlm_input"),
                ("VLM output", "vlm_output"),
            ):
                v = prices.get(key)
                if v is not None:
                    lines.append(f"- {label}: ${v:.2f}")
        for note in c.get("notes", []):
            lines.append(f"- _Note: {note}_")
    lines.append("")

    lines.append("## Coarse-to-fine VLM savings")
    lines.append("")
    lines.append(
        "Compares actual high-detail `get_step_detail` cost against the naive"
        " baseline of inspecting every step at high detail. Per-step low-detail"
        " preprocessing cost is shared by both and is excluded from this diff."
    )
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(
        f"| Mean step_count / run | {ca.get('mean_step_count', 0.0):.1f} |"
    )
    lines.append(
        f"| Mean high-detail VLM calls / run | {ca.get('mean_high_detail_calls_per_run', 0.0):.2f} |"
    )
    lines.append(
        f"| Actual high-detail VLM tokens (total) | {ca.get('actual_high_detail_tokens', 0)} |"
    )
    lines.append(
        f"| Naive high-detail VLM tokens (total, hypothetical) | {ca.get('naive_high_detail_tokens', 0)} |"
    )
    lines.append(
        f"| **Savings ratio** | **{_format_pct(ca.get('savings_ratio', 0.0))}** |"
    )
    lines.append("")

    if report.per_category:
        lines.append("## Per-category breakdown")
        lines.append("")
        lines.append("Binary acc is the primary signal. failure_type acc is advisory (see below).")
        lines.append("")
        lines.append("| Category | N | Binary acc | failure_type acc (advisory) | Mean tool_calls | Mean latency (s) |")
        lines.append("|---|---|---|---|---|---|")
        for cat, c in report.per_category.items():
            lines.append(
                f"| `{cat}` | {c['n']} "
                f"| {_format_pct(c['binary_accuracy'])} "
                f"| {_format_pct(c['failure_type_accuracy'])} "
                f"| {c['mean_tool_call_count']:.2f} "
                f"| {c['mean_latency_s']:.2f} |"
            )
        lines.append("")

    # ---- Advisory: failure_type & failure_step ----
    # Demoted from headline. Kept for qualitative observation only.
    lines.append("## Advisory: failure-type classification")
    lines.append("")
    lines.append(
        "**Not a primary quality signal.** The 5-class `failure_type` taxonomy"
        " has overlapping definitions (e.g. `inefficient_search` vs"
        " `missed_constraint` overlap in many real trajectories) and high"
        " inter-annotator noise — some samples are genuinely hard even for"
        " humans to classify. Reported here for qualitative observation only;"
        " do not use as a headline number."
    )
    lines.append("")
    lines.append("| Method | failure_type top-1 | N | Notes |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| Random baseline (uniform over {bl.get('vocabulary_size', 5)} classes) "
        f"| {_format_pct(bl.get('random_baseline_expected_accuracy', 0.0))} "
        f"| {bl.get('n_failed_samples', 0)} | E[hit] = mean(\\|label_set\\| / vocab_size) |"
    )
    lines.append(
        f"| Majority baseline (`{bl.get('majority_class') or '—'}`) "
        f"| {_format_pct(bl.get('majority_baseline_accuracy', 0.0))} "
        f"| {bl.get('n_failed_samples', 0)} | Always predicts dominant class |"
    )
    lines.append(
        f"| Agent | {_format_pct(m.get('failure_type_top1_accuracy', 0.0))} "
        f"| {m.get('failure_type_n', 0)} | Multi-label OR policy |"
    )
    lines.append("")
    lines.append(
        f"`failure_step` localization (±2): "
        f"**{_format_pct(m.get('failure_step_localization_within_2', 0.0))}** "
        f"(N={m.get('failure_step_n', 0)}) — also advisory; depends on the"
        f" agent picking the same root-cause step a human did, subject to the"
        f" same multi-failure ambiguity as failure_type."
    )
    lines.append("")

    if ev.get("total_evidence_items"):
        lines.append("## Evidence quality")
        lines.append("")
        lines.append(
            f"- Total `EvidenceItem`s across runs: **{ev.get('total_evidence_items', 0)}**"
        )
        lines.append(
            f"- `source=\"unavailable\"` items: {ev.get('unavailable_items', 0)} "
            f"(in {ev.get('samples_with_unavailable', 0)} runs — agent honestly flagged missing evidence)"
        )
        lines.append("")
        lines.append("| `source` | count |")
        lines.append("|---|---|")
        for k, v in sorted(
            ev.get("source_distribution", {}).items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"| `{k}` | {v} |")
        lines.append("")

    dist = m.get("terminated_by_distribution", {})
    if dist:
        lines.append("## Termination reasons")
        lines.append("")
        lines.append("| terminated_by | count |")
        lines.append("|---|---|")
        for k, v in sorted(dist.items()):
            lines.append(f"| `{k}` | {v} |")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Primary metric is `binary_verdict_accuracy`.** `failure_type` top-1"
        " and `failure_step` ±2 are reported as advisory only. The 5-class"
        " failure_type taxonomy has overlapping definitions, and the source"
        " labels carry non-trivial inter-annotator noise — treating them as"
        " a quality scoreboard conflates agent capability with labeling noise."
    )
    lines.append(
        "- Multi-label OR grading (for the advisory failure_type metric): a"
        " failed sample is correct iff the agent's single proposed"
        " `failure_type` appears in the labeled set (`;`-separated). Loosens"
        " the metric but does not lift the inter-annotator noise floor."
    )
    lines.append(
        "- Per-class precision/recall is **not** reported: per-class N (1–4 in the"
        " v1 golden set) is too small for class-level numbers to be meaningful."
    )
    lines.append(
        "- `failure_step` localization is only computed when both the label and the"
        " agent's proposal carry a step value, and the sample is labeled `failed`."
    )
    lines.append(
        "- Samples whose `run_id` is not importable into storage are excluded from"
        " all metrics; their count is reported in the `skipped` block."
    )
    lines.append(
        "- Baselines (`Random`, `Majority`) are computed analytically from the label"
        " distribution and do not involve any agent run; same numbers regardless of"
        " mock vs real LLM."
    )
    for note in report.notes:
        lines.append(f"- {note}")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def write_mock_smoke_test(
    graded: list[GradedSample],
    skipped: SkippedCounts,
    output_dir: Path,
) -> Path:
    """Write the mock-mode smoke-test artifact.

    Intentionally separate from ``write_report`` and emits a single JSON file
    at ``eval/_mock_smoke_test.json``. The leading underscore distinguishes
    it from production artifacts. Contains only enough to verify wiring:
    sample counts, termination distribution, and a top-level
    ``warning`` field. No accuracy claims.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    smoke_path = output_dir / "_mock_smoke_test.json"
    terminated_by_dist: dict[str, int] = {}
    for s in graded:
        terminated_by_dist[s.terminated_by] = (
            terminated_by_dist.get(s.terminated_by, 0) + 1
        )
    payload = {
        "warning": (
            "PIPELINE WIRING ONLY — these numbers reflect the OfflineAgentMock's"
            " hardcoded output (always proposes failure_type='missed_constraint'),"
            " NOT any diagnostic capability. Do not cite as a quality measure."
            " Use `python -m backend.app.agent_eval` (without --mock) for the real"
            " evaluation."
        ),
        "graded_n": len(graded),
        "skipped": skipped.to_dict(),
        "terminated_by_distribution": terminated_by_dist,
        "all_graded_terminated_via_propose": all(
            s.terminated_by == TERMINAL_TOOL for s in graded
        ),
    }
    smoke_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return smoke_path


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("data/triage_notes.csv"),
        help="Path to the golden-set CSV.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("eval"),
        help="Directory to write agent_report.{json,md}.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help=(
            "Force the OfflineAgentMock backend regardless of env vars. Useful for"
            " smoke-testing the grading pipeline without API quota."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only grade the first N samples (debugging).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Override the data root for this run. Defaults to the value of"
            " TRAJECTA_DATA_DIR (or <repo>/data when unset) — i.e. the same"
            " dataset the app uses. Pass an explicit path to redirect to an"
            " ad-hoc location for one-off experiments."
        ),
    )
    parser.add_argument(
        "--agent-price-input", type=float, default=None,
        help="Override agent-model input price (USD per 1M tokens). Falls back to internal price table.",
    )
    parser.add_argument(
        "--agent-price-output", type=float, default=None,
        help="Override agent-model output price (USD per 1M tokens).",
    )
    parser.add_argument(
        "--vlm-price-input", type=float, default=None,
        help="Override VLM input price (USD per 1M tokens).",
    )
    parser.add_argument(
        "--vlm-price-output", type=float, default=None,
        help="Override VLM output price (USD per 1M tokens).",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=None,
        help=(
            "Directory to dump per-sample AgentTrace JSONs (one file per"
            " run_id). When omitted and not --mock, defaults to"
            " <out>/runs/<archive_stamp>/traces — i.e. alongside the"
            " timestamped agent_report. When --mock is set and this flag"
            " is omitted, traces are NOT dumped (mock mode is a wiring"
            " smoke test, not a quality measurement). Pass an explicit"
            " path here to force dumping in mock mode."
        ),
    )
    args = parser.parse_args(argv)

    # Eval mode is the only mode this harness has. Auto-enable so users
    # don't have to remember `TRAJECTA_EVAL_MODE=1` on every invocation.
    # To deliberately run against unredacted production state (e.g. for
    # debugging a leak hypothesis), pre-export TRAJECTA_EVAL_MODE=0 before
    # calling the harness — anything other than the literal "1" is
    # treated as off by _is_eval_mode() in tools.py.
    if os.environ.get("TRAJECTA_EVAL_MODE") is None:
        os.environ["TRAJECTA_EVAL_MODE"] = "1"

    if args.data_dir is not None:
        resolved = args.data_dir.resolve()
        os.environ["TRAJECTA_DATA_DIR"] = str(resolved)
        os.environ["TRAJECTA_CHROMA_DIR"] = str(resolved / "chroma")
        # rag._client_cache may have been set during module import; bust it so
        # the new path is honored. Storage uses ``data_dir()`` per call so it
        # picks up the new env var without help.
        rag._client_cache = None  # type: ignore[attr-defined]
        print(f"--data-dir override → {resolved}", file=sys.stderr)
    print(
        f"Using TRAJECTA_DATA_DIR={os.environ.get('TRAJECTA_DATA_DIR', '<unset; storage will default to <repo>/data>')}",
        file=sys.stderr,
    )
    if os.getenv("TRAJECTA_EVAL_MODE"):
        print("TRAJECTA_EVAL_MODE=1 → eval-mode redaction active", file=sys.stderr)

    if not args.csv.exists():
        print(f"error: golden set CSV not found: {args.csv}", file=sys.stderr)
        return 2

    golden_raw = load_golden_set(args.csv)
    print(
        f"Loaded {len(golden_raw)} golden samples from {args.csv}",
        file=sys.stderr,
    )
    golden, golden_set_filter = filter_golden_set_for_failure_memory_overlap(golden_raw)
    print(
        "Golden set filter: "
        f"excluded_failure_memory_overlap={golden_set_filter.failure_memory_overlap_n}, "
        f"evaluating={golden_set_filter.evaluated_n}",
        file=sys.stderr,
    )
    # FastAPI lifespan (which calls rag.hydrate_all on startup) does NOT run
    # under ``python -m``. Without this, the failure_memory / eval_cases /
    # successful_runs Chroma collections are empty, and search_* tools come back
    # with nothing — the agent has no RAG context and the eval is degenerate.
    rag.hydrate_all()
    print("Hydrated ChromaDB collections from disk.", file=sys.stderr)

    agent_mode = "mock" if args.mock else "auto"
    # Capture eval start at the point we begin LLM work, not at process
    # start. Hydrate / argparse should not be billed against wall_clock.
    eval_started_at = datetime.now(timezone.utc)
    eval_start_perf = time.perf_counter()
    # archive_stamp is also used below to write the timestamped
    # agent_report archive. Computed here (before grading) so the trace
    # dump dir can share the same stamp — keeping per-run artefacts
    # co-located under eval/runs/<stamp>/.
    archive_stamp = eval_started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    if args.trace_dir is not None:
        # Explicit path always honored, in either mode.
        resolved_trace_dir: Path | None = args.trace_dir
    elif args.mock:
        # Mock mode is a wiring smoke test; default to no dumping so the
        # eval/runs/<stamp>/ tree stays a "real eval" surface.
        resolved_trace_dir = None
    else:
        resolved_trace_dir = args.out / "runs" / archive_stamp / "traces"
    if resolved_trace_dir is not None:
        print(f"Trace dumps → {resolved_trace_dir}", file=sys.stderr)
    graded, skipped = collect_graded_samples(
        golden,
        force_mock=args.mock,
        limit=args.limit,
        trace_dir=resolved_trace_dir,
    )
    eval_finished_at = datetime.now(timezone.utc)
    wall_clock_total_s = round(time.perf_counter() - eval_start_perf, 3)

    if args.mock:
        # Mock mode is a wiring smoke test, NOT a quality measurement. Skip
        # the production report entirely so the file name `agent_report.md`
        # is reserved for the real-LLM eval and cannot be mistaken for one.
        smoke_path = write_mock_smoke_test(graded, skipped, args.out)
        print(
            f"[mock] Pipeline wiring OK: graded={len(graded)}, "
            f"skipped(not_importable)={skipped.not_importable}, "
            f"skipped(agent_error)={skipped.agent_error}.",
            file=sys.stderr,
        )
        print(
            f"[mock] Wrote {smoke_path}. NOT a quality report — run without "
            f"--mock for actual metrics.",
            file=sys.stderr,
        )
        return 0

    # Compute baselines over the EXACT subset the agent was graded against,
    # so the baseline N matches the agent N. Otherwise the comparison column is
    # apples-to-oranges (e.g. baseline N=14 vs agent N=11 when 3 runs are not
    # importable). Restrict by the graded_run_ids set; baselines remain purely
    # analytical (no agent call).
    graded_run_ids = {s.run_id for s in graded}
    golden_graded = [s for s in golden if s.run_id in graded_run_ids]
    label_baselines = compute_label_baselines(golden_graded)
    report = build_report(
        graded,
        skipped,
        agent_mode=agent_mode,
        label_baselines=label_baselines,
        golden_set_filter=golden_set_filter,
    )

    # Decorate the report with run identity + cost. Models come from env
    # (they're what eval_agent_graph reads at construction time, same
    # vars). Token totals live on report.metrics already.
    agent_model = os.getenv("TRAJECTA_AGENT_MODEL")
    vlm_model = os.getenv("TRAJECTA_VLM_MODEL")
    cost_usd = _compute_cost_usd(
        agent_input_tokens=int(report.metrics.get("total_input_tokens", 0)),
        agent_output_tokens=int(report.metrics.get("total_output_tokens", 0)),
        vlm_input_tokens=int(report.metrics.get("total_vlm_input_tokens", 0)),
        vlm_output_tokens=int(report.metrics.get("total_vlm_output_tokens", 0)),
        agent_model=agent_model,
        vlm_model=vlm_model,
        overrides={
            "agent_input": args.agent_price_input,
            "agent_output": args.agent_price_output,
            "vlm_input": args.vlm_price_input,
            "vlm_output": args.vlm_price_output,
        },
    )
    report.started_at_utc = eval_started_at.isoformat(timespec="seconds")
    report.finished_at_utc = eval_finished_at.isoformat(timespec="seconds")
    report.wall_clock_total_s = wall_clock_total_s
    report.agent_model = agent_model
    report.vlm_model = vlm_model
    prompt_bundle = prompts.active_prompt_bundle()
    report.prompt_version = prompt_bundle.version
    report.prompt_sha256 = prompt_bundle.sha256
    vlm_prompt_bundle = prompts.active_vlm_high_detail_prompt()
    report.vlm_high_detail_prompt_version = vlm_prompt_bundle.version
    report.vlm_high_detail_prompt_sha256 = vlm_prompt_bundle.sha256
    report.cost_usd = cost_usd

    # Write a timestamped archive at <out>/runs/<ts>/agent_report.{json,md}
    # so each eval run is preserved and comparable. ``archive_stamp`` was
    # computed earlier in main() so the trace-dump dir could share it.
    archive_dir = args.out / "runs" / archive_stamp
    archive_json_path, archive_md_path = write_report(report, archive_dir)
    # Mirror to <out>/agent_report.{json,md} as the "latest" pointer that
    # tooling and humans bookmark. Plain copy (not symlink) for portability.
    latest_json = args.out / "agent_report.json"
    latest_md = args.out / "agent_report.md"
    args.out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive_json_path, latest_json)
    shutil.copyfile(archive_md_path, latest_md)

    print(
        f"Graded {len(graded)} / {len(golden)} samples ("
        f"skipped not_importable={skipped.not_importable},"
        f" agent_error={skipped.agent_error})",
        file=sys.stderr,
    )
    print(
        f"Wrote archive: {archive_json_path}\n"
        f"        and:  {archive_md_path}\n"
        f"Mirrored to latest: {latest_json}, {latest_md}",
        file=sys.stderr,
    )
    if resolved_trace_dir is not None:
        # Count what actually landed on disk (not what we tried to dump);
        # _dump_trace swallows OSError so a partial-disk failure must not
        # be silently overstated here.
        dumped = (
            len(list(resolved_trace_dir.glob("*.json")))
            if resolved_trace_dir.is_dir()
            else 0
        )
        print(
            f"Dumped {dumped} trace JSONs to {resolved_trace_dir}",
            file=sys.stderr,
        )
    if cost_usd.get("total_usd") is not None:
        print(f"Total cost: ${cost_usd['total_usd']:.4f} USD", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
