from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app import ragas_eval
from backend.app.schemas import AgentTrace, AgentTraceEvent, Trajectory


def _proposal_event(seq: int = 0) -> AgentTraceEvent:
    return AgentTraceEvent(
        seq=seq,
        type="tool_call",
        name="propose_eval_case",
        args={
            "actual_behavior": "The agent stopped early.",
            "failure_type": "early_terminated",
            "retrieved_context_ids": ["fm_early_terminated_001"],
            "evidence": [
                {
                    "claim": "Step 0 ended before the task was complete.",
                    "source": "trajectory",
                    "trajectory_id": "run_1",
                    "step_index": 0,
                }
            ],
        },
    )


def _success_proposal_event(seq: int = 0) -> AgentTraceEvent:
    """Success-shape proposal: omits the five failure fields (including
    actual_behavior) per the EvalCase contract; carries only evidence +
    retrieved_context_ids. Mirrors what the agent emits when it finds no
    concrete failure."""
    return AgentTraceEvent(
        seq=seq,
        type="tool_call",
        name="propose_eval_case",
        args={
            "retrieved_context_ids": ["fm_early_terminated_001"],
            "evidence": [
                {
                    "claim": "The final page satisfies the task.",
                    "source": "step_detail_high",
                    "trajectory_id": "run_1",
                    "step_index": 5,
                }
            ],
        },
    )


def _rag_call_event(seq: int = 0, *, query: str = "why did it stop early?") -> AgentTraceEvent:
    return AgentTraceEvent(
        seq=seq,
        type="tool_call",
        name="search_failure_memory",
        args={"query": query},
    )


def _rag_result_event(seq: int = 1, *, case_id: str = "fm_early_terminated_001") -> AgentTraceEvent:
    return AgentTraceEvent(
        seq=seq,
        type="tool_result",
        name="search_failure_memory",
        result={
            "items": [
                {
                    "case_id": case_id,
                    "summary": "Step 0 ended before the task was complete.",
                    "tags": ["termination"],
                }
            ]
        },
    )


def _trace(
    *,
    trajectory_id: str = "run_1",
    terminated_by: str = "propose_eval_case",
    events: list[AgentTraceEvent] | None = None,
) -> AgentTrace:
    """Minimal AgentTrace with a propose_eval_case event."""
    if events is None:
        events = [_rag_call_event(0), _rag_result_event(1), _proposal_event(2)]
    return AgentTrace(
        trajectory_id=trajectory_id,
        user_intent="analyze_trajectory",
        terminated_by=terminated_by,
        events=events,
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )


def _fake_run(trajectory_id: str) -> Trajectory:
    """Stand-in for ``storage.list_trajectories`` results — only ``trajectory_id`` is
    read by the loader's discovery set."""
    return Trajectory(trajectory_id=trajectory_id, task="t", steps=[])


def _sample() -> ragas_eval.RagasSample:
    return ragas_eval.RagasSample(
        trajectory_id="run_1",
        question="why did it stop early?",
        answer="The agent stopped early.\n\nStep 0 ended before the task was complete.",
        contexts=["fm_early_terminated_001: early stop [termination]"],
        ground_truth_source=ragas_eval.GROUND_TRUTH_SOURCE_NONE,
        proposed_failure_type="early_terminated",
        retrieved_context_ids=["fm_early_terminated_001"],
        tool_name="search_failure_memory",
        tool_query="why did it stop early?",
    )


class RagasEvalTests(unittest.TestCase):
    def test_ragas_answer_from_trace_rejects_non_terminal_trace(self) -> None:
        trace = AgentTrace(
            trajectory_id="run_1",
            user_intent="analyze_trajectory",
            terminated_by="budget_exceeded",
            events=[_proposal_event()],
        )

        with self.assertRaisesRegex(ValueError, "terminate via propose_eval_case"):
            ragas_eval.ragas_answer_from_trace(trace)

    def test_ragas_answer_from_trace_success_shape_returns_claims_only(self) -> None:
        """Success-shape drafts omit actual_behavior, so the RAGAS answer is the
        evidence claims alone — no leading actual_behavior, no blank-line prefix,
        and no crash."""
        trace = _trace(
            events=[_rag_call_event(0), _rag_result_event(1), _success_proposal_event(2)]
        )

        answer = ragas_eval.ragas_answer_from_trace(trace)

        self.assertEqual(answer, "The final page satisfies the task.")
        self.assertFalse(answer.startswith("\n"))
        self.assertNotIn("\n\n", answer)

    def test_ragas_answer_from_trace_tolerates_null_actual_behavior(self) -> None:
        """Exact crash repro: actual_behavior present but null (the shape the
        agent emits for a success draft) must not raise NoneType + str."""
        proposal = AgentTraceEvent(
            seq=2,
            type="tool_call",
            name="propose_eval_case",
            args={
                "actual_behavior": None,
                "retrieved_context_ids": ["fm_early_terminated_001"],
                "evidence": [
                    {
                        "claim": "Task completed successfully.",
                        "source": "step_detail_high",
                        "trajectory_id": "run_1",
                        "step_index": 3,
                    }
                ],
            },
        )
        trace = _trace(events=[_rag_call_event(0), _rag_result_event(1), proposal])

        answer = ragas_eval.ragas_answer_from_trace(trace)

        self.assertEqual(answer, "Task completed successfully.")

    def test_build_report_records_real_ragas_fallback_reason(self) -> None:
        sample = _sample()

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with mock.patch("backend.app.ragas_eval._ragas_import_failure", return_value=None):
                with mock.patch(
                    "backend.app.ragas_eval._run_real_ragas",
                    side_effect=RuntimeError("model unavailable"),
                ):
                    report = ragas_eval.build_report([sample], ragas_eval.SkippedCounts())

        self.assertEqual(report.ragas_mode, "stub")
        self.assertIsNotNone(report.fallback_reason)
        self.assertIn("real ragas evaluation failed", report.fallback_reason)
        self.assertIn("model unavailable", report.fallback_reason)


class RetrievalStatsTests(unittest.TestCase):
    def test_build_retrieval_stats_counts_tools_and_context_ids(self) -> None:
        stats = ragas_eval.build_retrieval_stats(
            [
                {
                    "trajectory_id": "run_1",
                    "tool_name": "search_failure_memory",
                    "contexts": [
                        "fm_a: First memory. [tag]",
                        "fm_b: Second memory. [tag]",
                    ],
                    "retrieved_context_ids": ["fm_a", "fm_c"],
                },
                {
                    "trajectory_id": "run_2",
                    "tool_name": "search_failure_eval_cases",
                    "contexts": ["ec_1: Eval case. [case]"],
                    "retrieved_context_ids": [],
                },
                {
                    "trajectory_id": "run_3",
                    "tool_name": "search_failure_memory",
                    "contexts": ["fm_a: Repeated memory. [tag]"],
                    "retrieved_context_ids": ["fm_a"],
                },
            ]
        )

        # by_tool keeps only the genuinely per-tool/per-sample counts.
        self.assertEqual(
            stats["by_tool"]["search_failure_memory"],
            {"sample_count": 2, "retrieved_context_count": 3},
        )
        self.assertEqual(
            stats["by_tool"]["search_failure_eval_cases"],
            {"sample_count": 1, "retrieved_context_count": 1},
        )

        # Retrieved-context histogram (global, from rendered contexts).
        self.assertEqual(
            stats["evidence_context_occurrences"],
            {"fm_a": 2, "ec_1": 1, "fm_b": 1},
        )

        # Cited ids are proposal-level: deduped per trace, not charged to a tool.
        self.assertEqual(
            stats["cited_context_ids"],
            {
                "trace_count": 3,
                "unique_cited_context_ids": ["fm_a", "fm_c"],
                "cited_context_id_trace_counts": {"fm_a": 2, "fm_c": 1},
                "cited_context_id_mentions": 3,
            },
        )

    def test_build_retrieval_stats_dedupes_cited_ids_per_trace(self) -> None:
        # One trace makes two RAG calls; both samples carry the same proposal
        # cited ids. They must be counted once per trace, not once per sample.
        stats = ragas_eval.build_retrieval_stats(
            [
                {
                    "trajectory_id": "run_x",
                    "tool_name": "search_failure_memory",
                    "contexts": ["fm_a: First. [tag]", "fm_b: Second. [tag]"],
                    "retrieved_context_ids": ["fm_a", "fm_b"],
                },
                {
                    "trajectory_id": "run_x",
                    "tool_name": "search_failure_eval_cases",
                    "contexts": ["ec_1: Eval case. [case]"],
                    "retrieved_context_ids": ["fm_a", "fm_b"],
                },
            ]
        )

        self.assertEqual(stats["by_tool"]["search_failure_memory"]["sample_count"], 1)
        self.assertEqual(stats["by_tool"]["search_failure_eval_cases"]["sample_count"], 1)
        self.assertEqual(
            stats["cited_context_ids"],
            {
                "trace_count": 1,
                "unique_cited_context_ids": ["fm_a", "fm_b"],
                # 1 each — not 2 — despite appearing in both samples.
                "cited_context_id_trace_counts": {"fm_a": 1, "fm_b": 1},
                "cited_context_id_mentions": 2,
            },
        )

    def test_build_retrieval_stats_includes_unused_search_tools(self) -> None:
        stats = ragas_eval.build_retrieval_stats(
            [
                {
                    "trajectory_id": "run_1",
                    "tool_name": "search_failure_memory",
                    "contexts": ["fm_a: First memory. [tag]"],
                    "retrieved_context_ids": ["fm_a"],
                }
            ]
        )

        self.assertEqual(
            stats["by_tool"]["search_failure_eval_cases"],
            {"sample_count": 0, "retrieved_context_count": 0},
        )

    def test_report_from_json_payload_recomputes_missing_retrieval_stats(self) -> None:
        report = ragas_eval.report_from_json_payload(
            {
                "samples": [
                    {
                        "trajectory_id": "run_1",
                        "tool_name": "search_failure_memory",
                        "contexts": ["fm_a: First memory. [tag]"],
                        "retrieved_context_ids": ["fm_a"],
                    }
                ],
                "metric_means": {"faithfulness": 0.25},
                "skipped": {
                    "budget_exceeded": 0,
                    "error": 1,
                    "no_trace": 2,
                    "no_context": 3,
                },
                "ground_truth_source": "none",
                "ragas_mode": "real",
            }
        )

        self.assertEqual(report.ragas_mode, "real")
        self.assertEqual(report.skipped.error, 1)
        self.assertEqual(
            report.retrieval_stats["by_tool"]["search_failure_memory"][
                "retrieved_context_count"
            ],
            1,
        )

    def test_write_report_renders_aggregate_retrieval_stats_only(self) -> None:
        report = ragas_eval.RagasReport(
            samples=[
                {
                    "trajectory_id": "run_1",
                    "tool_name": "search_failure_memory",
                    "contexts": ["fm_early_terminated_001: Early stop. [tag]"],
                    "retrieved_context_ids": ["fm_early_terminated_001"],
                }
            ],
            metric_means={"faithfulness": 0.25},
            skipped=ragas_eval.SkippedCounts(),
            ragas_mode="real",
        )

        with tempfile.TemporaryDirectory() as tmp:
            _, md_path = ragas_eval.write_report(report, Path(tmp))
            md = md_path.read_text(encoding="utf-8")

        self.assertIn("## Retrieval evidence summary", md)
        self.assertIn(
            "Retrieved contexts are what the RAG tools returned; cited context ids",
            md,
        )
        self.assertIn("| `search_failure_memory` | 1 | 1 |", md)
        self.assertIn("| `search_failure_eval_cases` | 0 | 0 |", md)
        self.assertIn("| `fm_early_terminated_001` | 1 |", md)
        self.assertIn("### Cited context ids", md)
        self.assertIn("- Traces with a proposal: 1", md)
        self.assertIn("- Unique cited context ids: `fm_early_terminated_001`", md)
        self.assertIn(
            "- Total cited-id references (deduped per trace): 1", md
        )
        self.assertNotIn("run_1", md)


# ---------------------------------------------------------------------------
# A6.1 — trace-loading precedence + discovery
#
# These tests pin the trace-dir-first / SQLite-fallback rule documented
# in ``backend/app/ragas_eval.py`` module docstring § Trace sources. All
# storage / DB access is mocked; no real ``data/trajecta.db`` is read.


class TraceLoadingPrecedenceTests(unittest.TestCase):
    """One trajectory_id → which source wins?"""

    def test_load_trace_for_trajectory_id_returns_sqlite_trace_when_present(self) -> None:
        """Without ``--trace-dir``, SQLite remains the source."""
        sqlite_trace = _trace(trajectory_id="run_x")
        with mock.patch(
            "backend.app.ragas_eval.storage.load_trace",
            return_value=sqlite_trace,
        ) as mock_load:
            result = ragas_eval.load_trace_for_trajectory_id("run_x")
        self.assertIs(result, sqlite_trace)
        mock_load.assert_called_once_with("run_x")

    def test_load_trace_for_trajectory_id_falls_back_to_trace_dir_when_sqlite_missing(
        self,
    ) -> None:
        """``--trace-dir`` is the Phase 8 A2 dump source. When it has a
        file for the trajectory_id, that file must be honoured."""
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            dumped = _trace(trajectory_id="run_x")
            (trace_dir / "run_x.json").write_text(
                dumped.model_dump_json(indent=2), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=None,
            ):
                result = ragas_eval.load_trace_for_trajectory_id(
                    "run_x", trace_dir=trace_dir
                )
        self.assertIsNotNone(result)
        self.assertEqual(result.trajectory_id, "run_x")
        self.assertEqual(result.terminated_by, "propose_eval_case")

    def test_load_trace_for_trajectory_id_prefers_trace_dir_over_sqlite(self) -> None:
        """Sanity: when both sources carry a trace for the same trajectory_id,
        the explicit trace-dir dump wins. Formal A6 should stay bound to
        the selected agent_eval artefact set."""
        sqlite_trace = _trace(trajectory_id="run_x")
        dumped = _trace(trajectory_id="run_x", terminated_by="budget_exceeded", events=[])
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_x.json").write_text(
                dumped.model_dump_json(indent=2), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=sqlite_trace,
            ):
                result = ragas_eval.load_trace_for_trajectory_id(
                    "run_x", trace_dir=trace_dir
                )
        self.assertIsNot(result, sqlite_trace)
        self.assertEqual(result.terminated_by, "budget_exceeded")

    def test_load_trace_for_trajectory_id_returns_none_when_neither_source_has_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=None
            ):
                self.assertIsNone(
                    ragas_eval.load_trace_for_trajectory_id("run_missing", trace_dir=trace_dir)
                )
                self.assertIsNone(
                    ragas_eval.load_trace_for_trajectory_id("run_missing")
                )


class DiscoverRunIdsTests(unittest.TestCase):
    """The discovery set is the union of SQLite-resident runs and
    trace-dir filenames. Both sides must contribute."""

    def test_discover_trajectory_ids_unions_sqlite_and_trace_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_dump_only.json").write_text("{}", encoding="utf-8")
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories",
                return_value=[_fake_run("run_sqlite_only"), _fake_run("run_both")],
            ):
                # Add the second run as both SQLite-resident AND
                # trace-dir-resident so we can verify de-dup.
                (trace_dir / "run_both.json").write_text("{}", encoding="utf-8")
                ids = ragas_eval._discover_trajectory_ids(trace_dir=trace_dir)
        self.assertEqual(ids, ["run_both", "run_dump_only", "run_sqlite_only"])

    def test_discover_trajectory_ids_sqlite_only_when_no_trace_dir(self) -> None:
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_a"), _fake_run("run_b")],
        ):
            self.assertEqual(
                ragas_eval._discover_trajectory_ids(trace_dir=None), ["run_a", "run_b"]
            )

    def test_discover_trajectory_ids_tolerates_storage_error(self) -> None:
        """A fresh checkout without a populated SQLite DB should not
        abort the eval — the trace-dir fallback still has work to do."""
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_dump.json").write_text("{}", encoding="utf-8")
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories",
                side_effect=RuntimeError("no DB"),
            ):
                ids = ragas_eval._discover_trajectory_ids(trace_dir=trace_dir)
        self.assertEqual(ids, ["run_dump"])


class CollectSamplesA61Tests(unittest.TestCase):
    """End-to-end: trace source precedence + skipped-count buckets."""

    def test_collect_samples_sqlite_trace_yields_one_sample(self) -> None:
        sqlite_trace = _trace(trajectory_id="run_sqlite")
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_sqlite")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=sqlite_trace,
            ):
                samples, skipped = ragas_eval.collect_samples()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].trajectory_id, "run_sqlite")
        self.assertEqual(samples[0].question, "why did it stop early?")
        self.assertEqual(samples[0].tool_query, "why did it stop early?")
        self.assertEqual(samples[0].tool_name, "search_failure_memory")
        self.assertEqual(
            samples[0].contexts,
            ["fm_early_terminated_001: Step 0 ended before the task was complete. [termination]"],
        )
        self.assertEqual(samples[0].ground_truth_source, ragas_eval.GROUND_TRUTH_SOURCE_NONE)
        self.assertEqual(samples[0].proposed_failure_type, "early_terminated")
        self.assertEqual(skipped.no_trace, 0)
        self.assertEqual(skipped.budget_exceeded, 0)
        self.assertEqual(skipped.error, 0)

    def test_collect_samples_success_shape_trace_yields_sample(self) -> None:
        """End-to-end repro of the success-shape crash: a success-shape draft
        that retrieved context must produce a RAGAS sample (answer = claims
        only) via collect_samples -> ragas_answer_from_trace, not raise."""
        trace = _trace(
            trajectory_id="run_success",
            events=[
                _rag_call_event(0),
                _rag_result_event(1),
                _success_proposal_event(2),
            ],
        )
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_success")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=trace,
            ):
                samples, skipped = ragas_eval.collect_samples()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].trajectory_id, "run_success")
        self.assertEqual(samples[0].answer, "The final page satisfies the task.")
        self.assertEqual(samples[0].proposed_failure_type, "")
        self.assertEqual(skipped.no_context, 0)

    def test_collect_samples_trace_dir_fallback_when_sqlite_empty(self) -> None:
        """Trace-dir-only run: storage has no trace, but the dump dir
        does. A6.1's primary win — the agent_eval flow's dumps are
        finally readable by RAGAS without round-tripping through the
        SQLite ``traces`` table."""
        dumped = _trace(trajectory_id="run_dumped")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_dumped.json").write_text(
                dumped.model_dump_json(indent=2), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories", return_value=[]
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace", return_value=None
                ):
                    samples, skipped = ragas_eval.collect_samples(
                        trace_dir=trace_dir
                    )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].trajectory_id, "run_dumped")
        self.assertEqual(skipped.no_trace, 0)

    def test_collect_samples_uses_matching_rag_tool_result_contexts(self) -> None:
        trace = _trace(
            trajectory_id="run_rag",
            events=[
                _rag_call_event(0, query="first query"),
                _rag_result_event(1, case_id="fm_first"),
                _rag_call_event(2, query="second query"),
                AgentTraceEvent(
                    seq=3,
                    type="tool_result",
                    name="search_failure_memory",
                    result={
                        "items": [
                            {
                                "case_id": "fm_second",
                                "summary": "Second context only.",
                                "tags": ["second"],
                            }
                        ]
                    },
                ),
                _proposal_event(4),
            ],
        )
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_rag")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=trace,
            ):
                samples, skipped = ragas_eval.collect_samples()

        self.assertEqual([s.question for s in samples], ["first query", "second query"])
        self.assertEqual(samples[0].contexts, ["fm_first: Step 0 ended before the task was complete. [termination]"])
        self.assertEqual(samples[1].contexts, ["fm_second: Second context only. [second]"])
        self.assertEqual(skipped.no_context, 0)

    def test_collect_samples_skips_no_trace_when_neither_source_has_one(
        self,
    ) -> None:
        """A run that lives in storage.list_trajectories but lacks a trace row
        (e.g. import-only, never analyzed) must increment
        ``no_trace`` — not silently disappear or be misfiled as
        ``error``."""
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_no_trace")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=None
            ):
                samples, skipped = ragas_eval.collect_samples()
        self.assertEqual(samples, [])
        self.assertEqual(skipped.no_trace, 1)
        self.assertEqual(skipped.error, 0)
        self.assertEqual(skipped.budget_exceeded, 0)

    def test_collect_samples_counts_budget_exceeded_trace(self) -> None:
        be_trace = _trace(
            trajectory_id="run_be", terminated_by="budget_exceeded", events=[]
        )
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_be")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=be_trace
            ):
                samples, skipped = ragas_eval.collect_samples()
        self.assertEqual(samples, [])
        self.assertEqual(skipped.budget_exceeded, 1)
        self.assertEqual(skipped.error, 0)
        self.assertEqual(skipped.no_trace, 0)
        self.assertEqual(skipped.no_context, 0)

    def test_collect_samples_counts_no_context_tool_call(self) -> None:
        trace = _trace(
            trajectory_id="run_empty_context",
            events=[
                _rag_call_event(0),
                AgentTraceEvent(
                    seq=1,
                    type="tool_result",
                    name="search_failure_memory",
                    result={"items": []},
                ),
                _proposal_event(2),
            ],
        )
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_empty_context")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=trace,
            ):
                samples, skipped = ragas_eval.collect_samples()
        self.assertEqual(samples, [])
        self.assertEqual(skipped.no_context, 1)

    def test_collect_samples_limit_caps_valid_samples(self) -> None:
        trace_a = _trace(trajectory_id="run_a")
        trace_b = _trace(trajectory_id="run_b")

        def fake_load_trace(trajectory_id: str):
            return {"run_a": trace_a, "run_b": trace_b}[trajectory_id]

        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_a"), _fake_run("run_b")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                side_effect=fake_load_trace,
            ):
                samples, skipped = ragas_eval.collect_samples(limit=1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].trajectory_id, "run_a")
        self.assertEqual(skipped.no_context, 0)

    def test_collect_samples_limit_does_not_truncate_skipped_counts(self) -> None:
        trace_a = _trace(trajectory_id="run_a")
        trace_b = _trace(
            trajectory_id="run_b",
            events=[_rag_call_event(0), _proposal_event(1)],
        )

        def fake_load_trace(trajectory_id: str):
            return {"run_a": trace_a, "run_b": trace_b}[trajectory_id]

        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_a"), _fake_run("run_b")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                side_effect=fake_load_trace,
            ):
                samples, skipped = ragas_eval.collect_samples(limit=1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].trajectory_id, "run_a")
        self.assertEqual(skipped.no_context, 1)

    def test_collect_samples_counts_malformed_trace_dir_file_as_error(self) -> None:
        """A trace-dir file that fails AgentTrace validation is an
        error, not no_trace — surfacing the corruption matters for
        operators who think their dump is intact."""
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_bad.json").write_text(
                json.dumps({"not": "a trace"}), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories", return_value=[]
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace", return_value=None
                ):
                    samples, skipped = ragas_eval.collect_samples(
                        trace_dir=trace_dir
                    )
        self.assertEqual(samples, [])
        self.assertEqual(skipped.error, 1)
        self.assertEqual(skipped.no_trace, 0)

    def test_collect_samples_skips_non_terminal_tool_trace_as_error(
        self,
    ) -> None:
        """A trace whose terminated_by isn't propose_eval_case or
        budget_exceeded counts as error — RAGAS needs the terminal
        tool args to extract the answer text."""
        error_trace = _trace(trajectory_id="run_err", terminated_by="error", events=[])
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_err")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=error_trace
            ):
                _, skipped = ragas_eval.collect_samples()
        self.assertEqual(skipped.error, 1)

    def test_collect_samples_processes_union_of_both_sources(self) -> None:
        """Mixed run: one sample from SQLite, one from trace dir,
        produced in the same RAGAS report. A6.1 explicitly supports
        operators who have both UI-driven analyzes and a bulk
        agent_eval dump on disk."""
        sqlite_trace = _trace(trajectory_id="run_a")
        dumped_trace = _trace(trajectory_id="run_b")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_b.json").write_text(
                dumped_trace.model_dump_json(indent=2), encoding="utf-8"
            )

            def fake_load_trace(trajectory_id: str):
                return sqlite_trace if trajectory_id == "run_a" else None

            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories",
                return_value=[_fake_run("run_a")],
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace",
                    side_effect=fake_load_trace,
                ):
                    samples, skipped = ragas_eval.collect_samples(
                        trace_dir=trace_dir
                    )
        self.assertEqual({s.trajectory_id for s in samples}, {"run_a", "run_b"})
        self.assertEqual(skipped.no_trace, 0)

    def test_collect_samples_ignores_legacy_data_runs_layout(self) -> None:
        """A6.1 retires the pre-storage-refactor
        ``data/runs/<id>/last_trace.json`` scan. Even when such files
        exist on disk, the loader must read SQLite + trace-dir only —
        legacy files no longer contribute samples or skipped counts."""
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            legacy = data_root / "runs" / "run_legacy"
            legacy.mkdir(parents=True)
            (legacy / "last_trace.json").write_text(
                _trace(trajectory_id="run_legacy").model_dump_json(indent=2),
                encoding="utf-8",
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories", return_value=[]
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace", return_value=None
                ):
                    samples, skipped = ragas_eval.collect_samples(data_root)
        # No discovery hit, so no skipped bucket either — A6.1's
        # discovery set excludes the legacy directory entirely.
        self.assertEqual(samples, [])
        self.assertEqual(skipped.no_trace, 0)
        self.assertEqual(skipped.error, 0)

    def test_collect_samples_ignores_ground_truth_fixture_for_a6(self) -> None:
        """A6 is no-ground-truth faithfulness, so disk fixtures must not
        turn the report back into answer-correctness or self-grading."""
        sqlite_trace = _trace(trajectory_id="run_gt")
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            gt_dir = data_root / "runs" / "run_gt"
            gt_dir.mkdir(parents=True)
            (gt_dir / "ground_truth.json").write_text(
                json.dumps({"failure_type": "missed_constraint"}), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories",
                return_value=[_fake_run("run_gt")],
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace",
                    return_value=sqlite_trace,
                ):
                    samples, _ = ragas_eval.collect_samples(data_root)
        self.assertEqual(len(samples), 1)
        self.assertEqual(
            samples[0].ground_truth_source,
            ragas_eval.GROUND_TRUTH_SOURCE_NONE,
        )


class MainTraceDirFlagTests(unittest.TestCase):
    """The CLI must accept ``--trace-dir`` and thread the resolved Path
    into ``collect_samples``. Without this flag the agent_eval flow's
    dumps would remain invisible to the RAGAS loader."""

    def test_main_passes_trace_dir_into_collect_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            trace_dir.mkdir()
            out_dir = Path(tmp) / "eval"
            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ) as mock_collect:
                with mock.patch(
                    "backend.app.ragas_eval.write_report",
                    return_value=(out_dir / "x.json", out_dir / "x.md"),
                ):
                    rc = ragas_eval.main(
                        [
                            "--data-dir",
                            str(Path(tmp) / "data"),
                            "--trace-dir",
                            str(trace_dir),
                            "--limit",
                            "10",
                            "--output-dir",
                            str(out_dir),
                            "--force-stub",
                        ]
                    )
        self.assertEqual(rc, 0)
        mock_collect.assert_called_once()
        _, kwargs = mock_collect.call_args
        self.assertEqual(kwargs["trace_dir"], trace_dir.resolve())
        self.assertEqual(kwargs["limit"], 10)

    def test_main_passes_none_when_trace_dir_flag_absent(self) -> None:
        """Default behaviour — no --trace-dir — still reaches SQLite
        only. Lock in that the flag is opt-in."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "eval"
            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ) as mock_collect:
                with mock.patch(
                    "backend.app.ragas_eval.write_report",
                    return_value=(out_dir / "x.json", out_dir / "x.md"),
                ):
                    rc = ragas_eval.main(
                        [
                            "--data-dir",
                            str(Path(tmp) / "data"),
                            "--output-dir",
                            str(out_dir),
                            "--force-stub",
                        ]
                    )
        self.assertEqual(rc, 0)
        _, kwargs = mock_collect.call_args
        self.assertIsNone(kwargs["trace_dir"])
        self.assertIsNone(kwargs["limit"])


class MainReportArchiveTests(unittest.TestCase):
    """main writes a stable latest report at the base dir AND a timestamped
    archive under ragas_report/<stamp>/ so prior runs are never overwritten
    (mirrors agent_eval's eval/agent_report.* + eval/runs/<stamp>/ pairing)."""

    def test_main_writes_latest_and_timestamped_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "eval"
            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ):
                rc = ragas_eval.main(
                    [
                        "--data-dir",
                        str(Path(tmp) / "data"),
                        "--output-dir",
                        str(out_dir),
                        "--force-stub",
                    ]
                )
            self.assertEqual(rc, 0)
            # main() resolves --output-dir (canonicalising macOS /tmp symlinks),
            # so assert against the resolved base — inside the with block, before
            # the TemporaryDirectory is cleaned up.
            base = out_dir.resolve()
            # Stable latest at the base dir.
            self.assertTrue((base / "ragas_report.md").is_file())
            self.assertTrue((base / "ragas_report.json").is_file())
            # Exactly one timestamped archive under ragas_report/<stamp>/.
            archived_md = sorted(base.glob("ragas_report/*/ragas_report.md"))
            self.assertEqual(len(archived_md), 1)
            stamp_dir = archived_md[0].parent
            self.assertTrue((stamp_dir / "ragas_report.json").is_file())
            # Stamp matches the agent_eval UTC convention (YYYY-MM-DDTHH-MM-SSZ).
            self.assertRegex(
                stamp_dir.name, r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$"
            )


class BuildReportStubFallbackTests(unittest.TestCase):
    """build_report continues to fall back to stub when OPENAI_API_KEY
    is absent — A6.1 must not silently flip the report to real mode."""

    def test_build_report_force_stub_without_openai_key(self) -> None:
        sample = _sample()
        with mock.patch.dict(os.environ, {}, clear=True):
            report = ragas_eval.build_report([sample], ragas_eval.SkippedCounts())
        self.assertEqual(report.ragas_mode, "stub")
        self.assertEqual(report.fallback_reason, "OPENAI_API_KEY is not set")


class ContextRecallReferenceTests(unittest.TestCase):
    """Reference loading + attachment for the context_recall metric."""

    def test_load_references_only_failed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            (data_root / "triage_notes.csv").write_text(
                "sample_id,category,outcome,failure_mode,failure_step,notes\n"
                "run_fail,github,failed,wrong_result;missed_constraint,5,forgot the stars filter\n"
                "run_succ,github,success,,,\n",
                encoding="utf-8",
            )
            refs = ragas_eval._load_references(data_root)
        self.assertIn("run_fail", refs)
        self.assertNotIn("run_succ", refs)
        # multi-label failure_mode rendered, plus the human note carried through.
        self.assertIn("wrong_result, missed_constraint", refs["run_fail"])
        self.assertIn("forgot the stars filter", refs["run_fail"])

    def test_load_references_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ragas_eval._load_references(Path(tmp)), {})

    def test_collect_samples_attaches_reference_from_triage(self) -> None:
        """A failed-golden trajectory's triage description must land on the
        sample as its context_recall reference."""
        trace = _trace(trajectory_id="run_ref")
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            (data_root / "triage_notes.csv").write_text(
                "sample_id,category,outcome,failure_mode,failure_step,notes\n"
                "run_ref,github,failed,wrong_result,5,picked the wrong repo\n",
                encoding="utf-8",
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories",
                return_value=[_fake_run("run_ref")],
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace",
                    return_value=trace,
                ):
                    samples, _ = ragas_eval.collect_samples(data_root)
        self.assertEqual(len(samples), 1)
        self.assertIn("wrong_result", samples[0].reference)
        self.assertIn("picked the wrong repo", samples[0].reference)

    def test_collect_samples_no_reference_when_not_in_triage(self) -> None:
        """A trajectory absent from triage (or success-shape) gets an empty
        reference and is therefore excluded from context_recall."""
        trace = _trace(trajectory_id="run_unlisted")
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            (data_root / "triage_notes.csv").write_text(
                "sample_id,category,outcome,failure_mode,failure_step,notes\n"
                "someone_else,github,failed,wrong_result,5,note\n",
                encoding="utf-8",
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_trajectories",
                return_value=[_fake_run("run_unlisted")],
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace",
                    return_value=trace,
                ):
                    samples, _ = ragas_eval.collect_samples(data_root)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].reference, "")


class MetricSelectionTests(unittest.TestCase):
    """--metric selects which RAGAS metric(s) the real path computes."""

    def test_build_report_forwards_metric_selection(self) -> None:
        sample = _sample()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            with mock.patch(
                "backend.app.ragas_eval._ragas_import_failure", return_value=None
            ):
                with mock.patch(
                    "backend.app.ragas_eval._run_real_ragas",
                    return_value={"context_recall": 0.5},
                ) as m:
                    report = ragas_eval.build_report(
                        [sample],
                        ragas_eval.SkippedCounts(),
                        metrics=("context_recall",),
                    )
        self.assertEqual(report.ragas_mode, "real")
        self.assertEqual(report.metric_means, {"context_recall": 0.5})
        _, kwargs = m.call_args
        self.assertEqual(kwargs["metrics"], ("context_recall",))

    def test_main_metric_flag_selects_context_recall_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "eval"
            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ):
                with mock.patch(
                    "backend.app.ragas_eval.build_report",
                    return_value=ragas_eval.RagasReport(),
                ) as mb:
                    with mock.patch(
                        "backend.app.ragas_eval.write_report",
                        return_value=(out_dir / "x.json", out_dir / "x.md"),
                    ):
                        rc = ragas_eval.main(
                            [
                                "--data-dir",
                                str(Path(tmp) / "data"),
                                "--output-dir",
                                str(out_dir),
                                "--context-mode",
                                "rag",
                                "--metric",
                                "context_recall",
                                "--force-stub",
                            ]
                        )
        self.assertEqual(rc, 0)
        _, kwargs = mb.call_args
        self.assertEqual(kwargs["metrics"], ("context_recall",))

    def test_main_metric_flag_defaults_to_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "eval"
            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ):
                with mock.patch(
                    "backend.app.ragas_eval.build_report",
                    return_value=ragas_eval.RagasReport(),
                ) as mb:
                    with mock.patch(
                        "backend.app.ragas_eval.write_report",
                        return_value=(out_dir / "x.json", out_dir / "x.md"),
                    ):
                        rc = ragas_eval.main(
                            [
                                "--data-dir",
                                str(Path(tmp) / "data"),
                                "--output-dir",
                                str(out_dir),
                                "--context-mode",
                                "rag",
                                "--force-stub",
                            ]
                        )
        self.assertEqual(rc, 0)
        _, kwargs = mb.call_args
        self.assertEqual(kwargs["metrics"], ("faithfulness", "context_recall"))


class MergeReportTests(unittest.TestCase):
    """--merge folds a freshly computed metric into the existing report
    without recomputing the metric already on disk."""

    def test_merge_keeps_existing_metric_and_adds_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = (Path(tmp) / "eval").resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            # Existing report already has faithfulness from a prior (slow) run.
            (out_dir / "ragas_report.json").write_text(
                json.dumps({"metric_means": {"faithfulness": 0.5}}),
                encoding="utf-8",
            )
            new_report = ragas_eval.RagasReport(metric_means={"context_recall": 0.7})
            captured: dict[str, dict] = {}

            def fake_write(report, output_dir):
                captured["means"] = dict(report.metric_means)
                return (
                    output_dir / "ragas_report.json",
                    output_dir / "ragas_report.md",
                )

            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ):
                with mock.patch(
                    "backend.app.ragas_eval.build_report", return_value=new_report
                ):
                    with mock.patch(
                        "backend.app.ragas_eval.write_report", side_effect=fake_write
                    ):
                        rc = ragas_eval.main(
                            [
                                "--data-dir",
                                str(Path(tmp) / "data"),
                                "--output-dir",
                                str(out_dir),
                                "--metric",
                                "context_recall",
                                "--merge",
                                "--force-stub",
                            ]
                        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            captured["means"], {"faithfulness": 0.5, "context_recall": 0.7}
        )

    def test_merge_without_existing_report_writes_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = (Path(tmp) / "eval").resolve()
            new_report = ragas_eval.RagasReport(metric_means={"context_recall": 0.7})
            captured: dict[str, dict] = {}

            def fake_write(report, output_dir):
                captured["means"] = dict(report.metric_means)
                return (
                    output_dir / "ragas_report.json",
                    output_dir / "ragas_report.md",
                )

            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ):
                with mock.patch(
                    "backend.app.ragas_eval.build_report", return_value=new_report
                ):
                    with mock.patch(
                        "backend.app.ragas_eval.write_report", side_effect=fake_write
                    ):
                        rc = ragas_eval.main(
                            [
                                "--data-dir",
                                str(Path(tmp) / "data"),
                                "--output-dir",
                                str(out_dir),
                                "--metric",
                                "context_recall",
                                "--merge",
                                "--force-stub",
                            ]
                        )
        self.assertEqual(rc, 0)
        # No prior report -> nothing to merge, just the new metric.
        self.assertEqual(captured["means"], {"context_recall": 0.7})


class EvidenceContextModeTests(unittest.TestCase):
    """--context-mode evidence builds contexts from agent-visible evidence
    (step-detail reads etc.), so traces that never called a RAG tool still score."""

    def _step_detail_trace(self, trajectory_id: str = "run_ev") -> AgentTrace:
        return _trace(
            trajectory_id=trajectory_id,
            events=[
                AgentTraceEvent(
                    seq=0,
                    type="tool_call",
                    name="get_step_detail",
                    args={"step_index": 5},
                ),
                AgentTraceEvent(
                    seq=1,
                    type="tool_result",
                    name="get_step_detail",
                    result={
                        "step_index": 5,
                        "vlm_summary": "The page shows a results list with prices.",
                    },
                ),
                _proposal_event(2),
            ],
        )

    def test_evidence_mode_uses_step_detail_without_any_rag_call(self) -> None:
        trace = self._step_detail_trace()
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_ev")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=trace
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_digest", return_value=None
                ):
                    samples, skipped = ragas_eval.collect_samples(context_mode="evidence")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].context_mode, "evidence")
        self.assertTrue(
            any("results list with prices" in c for c in samples[0].contexts)
        )
        self.assertEqual(skipped.no_context, 0)

    def test_evidence_mode_no_visible_evidence_skips(self) -> None:
        trace = _trace(trajectory_id="run_bare", events=[_proposal_event(0)])
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_bare")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=trace
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_digest", return_value=None
                ):
                    samples, skipped = ragas_eval.collect_samples(context_mode="evidence")
        self.assertEqual(samples, [])
        self.assertEqual(skipped.no_context, 1)

    def test_rag_mode_unchanged_default(self) -> None:
        """Default (rag) mode still produces the search-tool sample."""
        trace = _trace(trajectory_id="run_rag")
        with mock.patch(
            "backend.app.ragas_eval.storage.list_trajectories",
            return_value=[_fake_run("run_rag")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=trace
            ):
                samples, _ = ragas_eval.collect_samples()  # default context_mode="rag"
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].context_mode, "rag")
        self.assertEqual(samples[0].tool_name, "search_failure_memory")

    def test_main_evidence_mode_threads_and_drops_context_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = (Path(tmp) / "eval").resolve()
            with mock.patch(
                "backend.app.ragas_eval.collect_samples",
                return_value=([], ragas_eval.SkippedCounts()),
            ) as mc:
                with mock.patch(
                    "backend.app.ragas_eval.build_report",
                    return_value=ragas_eval.RagasReport(),
                ) as mb:
                    with mock.patch(
                        "backend.app.ragas_eval.write_report",
                        return_value=(out_dir / "x.json", out_dir / "x.md"),
                    ):
                        rc = ragas_eval.main(
                            [
                                "--data-dir",
                                str(Path(tmp) / "data"),
                                "--output-dir",
                                str(out_dir),
                                "--context-mode",
                                "evidence",
                                "--metric",
                                "both",
                                "--force-stub",
                            ]
                        )
        self.assertEqual(rc, 0)
        self.assertEqual(mc.call_args.kwargs["context_mode"], "evidence")
        # context_recall dropped in evidence mode -> faithfulness only.
        self.assertEqual(mb.call_args.kwargs["metrics"], ("faithfulness",))


if __name__ == "__main__":
    unittest.main()
