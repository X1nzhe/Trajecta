from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app import ragas_eval
from backend.app.schemas import AgentTrace, AgentTraceEvent, TrajectoryRun


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
                    "run_id": "run_1",
                    "step_index": 0,
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
    run_id: str = "run_1",
    terminated_by: str = "propose_eval_case",
    events: list[AgentTraceEvent] | None = None,
) -> AgentTrace:
    """Minimal AgentTrace with a propose_eval_case event."""
    if events is None:
        events = [_rag_call_event(0), _rag_result_event(1), _proposal_event(2)]
    return AgentTrace(
        run_id=run_id,
        user_intent="analyze_run",
        terminated_by=terminated_by,
        events=events,
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )


def _fake_run(run_id: str) -> TrajectoryRun:
    """Stand-in for ``storage.list_runs`` results — only ``run_id`` is
    read by the loader's discovery set."""
    return TrajectoryRun(run_id=run_id, task="t", steps=[])


def _sample() -> ragas_eval.RagasSample:
    return ragas_eval.RagasSample(
        run_id="run_1",
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
            run_id="run_1",
            user_intent="analyze_run",
            terminated_by="budget_exceeded",
            events=[_proposal_event()],
        )

        with self.assertRaisesRegex(ValueError, "terminate via propose_eval_case"):
            ragas_eval.ragas_answer_from_trace(trace)

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


# ---------------------------------------------------------------------------
# A6.1 — trace-loading precedence + discovery
#
# These tests pin the trace-dir-first / SQLite-fallback rule documented
# in ``backend/app/ragas_eval.py`` module docstring § Trace sources. All
# storage / DB access is mocked; no real ``data/trajecta.db`` is read.


class TraceLoadingPrecedenceTests(unittest.TestCase):
    """One run_id → which source wins?"""

    def test_load_trace_for_run_id_returns_sqlite_trace_when_present(self) -> None:
        """Without ``--trace-dir``, SQLite remains the source."""
        sqlite_trace = _trace(run_id="run_x")
        with mock.patch(
            "backend.app.ragas_eval.storage.load_trace",
            return_value=sqlite_trace,
        ) as mock_load:
            result = ragas_eval.load_trace_for_run_id("run_x")
        self.assertIs(result, sqlite_trace)
        mock_load.assert_called_once_with("run_x")

    def test_load_trace_for_run_id_falls_back_to_trace_dir_when_sqlite_missing(
        self,
    ) -> None:
        """``--trace-dir`` is the Phase 8 A2 dump source. When it has a
        file for the run_id, that file must be honoured."""
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            dumped = _trace(run_id="run_x")
            (trace_dir / "run_x.json").write_text(
                dumped.model_dump_json(indent=2), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=None,
            ):
                result = ragas_eval.load_trace_for_run_id(
                    "run_x", trace_dir=trace_dir
                )
        self.assertIsNotNone(result)
        self.assertEqual(result.run_id, "run_x")
        self.assertEqual(result.terminated_by, "propose_eval_case")

    def test_load_trace_for_run_id_prefers_trace_dir_over_sqlite(self) -> None:
        """Sanity: when both sources carry a trace for the same run_id,
        the explicit trace-dir dump wins. Formal A6 should stay bound to
        the selected agent_eval artefact set."""
        sqlite_trace = _trace(run_id="run_x")
        dumped = _trace(run_id="run_x", terminated_by="budget_exceeded", events=[])
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_x.json").write_text(
                dumped.model_dump_json(indent=2), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=sqlite_trace,
            ):
                result = ragas_eval.load_trace_for_run_id(
                    "run_x", trace_dir=trace_dir
                )
        self.assertIsNot(result, sqlite_trace)
        self.assertEqual(result.terminated_by, "budget_exceeded")

    def test_load_trace_for_run_id_returns_none_when_neither_source_has_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace", return_value=None
            ):
                self.assertIsNone(
                    ragas_eval.load_trace_for_run_id("run_missing", trace_dir=trace_dir)
                )
                self.assertIsNone(
                    ragas_eval.load_trace_for_run_id("run_missing")
                )


class DiscoverRunIdsTests(unittest.TestCase):
    """The discovery set is the union of SQLite-resident runs and
    trace-dir filenames. Both sides must contribute."""

    def test_discover_run_ids_unions_sqlite_and_trace_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_dump_only.json").write_text("{}", encoding="utf-8")
            with mock.patch(
                "backend.app.ragas_eval.storage.list_runs",
                return_value=[_fake_run("run_sqlite_only"), _fake_run("run_both")],
            ):
                # Add the second run as both SQLite-resident AND
                # trace-dir-resident so we can verify de-dup.
                (trace_dir / "run_both.json").write_text("{}", encoding="utf-8")
                ids = ragas_eval._discover_run_ids(trace_dir=trace_dir)
        self.assertEqual(ids, ["run_both", "run_dump_only", "run_sqlite_only"])

    def test_discover_run_ids_sqlite_only_when_no_trace_dir(self) -> None:
        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
            return_value=[_fake_run("run_a"), _fake_run("run_b")],
        ):
            self.assertEqual(
                ragas_eval._discover_run_ids(trace_dir=None), ["run_a", "run_b"]
            )

    def test_discover_run_ids_tolerates_storage_error(self) -> None:
        """A fresh checkout without a populated SQLite DB should not
        abort the eval — the trace-dir fallback still has work to do."""
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_dump.json").write_text("{}", encoding="utf-8")
            with mock.patch(
                "backend.app.ragas_eval.storage.list_runs",
                side_effect=RuntimeError("no DB"),
            ):
                ids = ragas_eval._discover_run_ids(trace_dir=trace_dir)
        self.assertEqual(ids, ["run_dump"])


class CollectSamplesA61Tests(unittest.TestCase):
    """End-to-end: trace source precedence + skipped-count buckets."""

    def test_collect_samples_sqlite_trace_yields_one_sample(self) -> None:
        sqlite_trace = _trace(run_id="run_sqlite")
        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
            return_value=[_fake_run("run_sqlite")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                return_value=sqlite_trace,
            ):
                samples, skipped = ragas_eval.collect_samples()
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].run_id, "run_sqlite")
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

    def test_collect_samples_trace_dir_fallback_when_sqlite_empty(self) -> None:
        """Trace-dir-only run: storage has no trace, but the dump dir
        does. A6.1's primary win — the agent_eval flow's dumps are
        finally readable by RAGAS without round-tripping through the
        SQLite ``traces`` table."""
        dumped = _trace(run_id="run_dumped")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_dumped.json").write_text(
                dumped.model_dump_json(indent=2), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_runs", return_value=[]
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace", return_value=None
                ):
                    samples, skipped = ragas_eval.collect_samples(
                        trace_dir=trace_dir
                    )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].run_id, "run_dumped")
        self.assertEqual(skipped.no_trace, 0)

    def test_collect_samples_uses_matching_rag_tool_result_contexts(self) -> None:
        trace = _trace(
            run_id="run_rag",
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
            "backend.app.ragas_eval.storage.list_runs",
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
        """A run that lives in storage.list_runs but lacks a trace row
        (e.g. import-only, never analyzed) must increment
        ``no_trace`` — not silently disappear or be misfiled as
        ``error``."""
        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
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
            run_id="run_be", terminated_by="budget_exceeded", events=[]
        )
        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
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
            run_id="run_empty_context",
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
            "backend.app.ragas_eval.storage.list_runs",
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
        trace_a = _trace(run_id="run_a")
        trace_b = _trace(run_id="run_b")

        def fake_load_trace(run_id: str):
            return {"run_a": trace_a, "run_b": trace_b}[run_id]

        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
            return_value=[_fake_run("run_a"), _fake_run("run_b")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                side_effect=fake_load_trace,
            ):
                samples, skipped = ragas_eval.collect_samples(limit=1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].run_id, "run_a")
        self.assertEqual(skipped.no_context, 0)

    def test_collect_samples_limit_does_not_truncate_skipped_counts(self) -> None:
        trace_a = _trace(run_id="run_a")
        trace_b = _trace(
            run_id="run_b",
            events=[_rag_call_event(0), _proposal_event(1)],
        )

        def fake_load_trace(run_id: str):
            return {"run_a": trace_a, "run_b": trace_b}[run_id]

        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
            return_value=[_fake_run("run_a"), _fake_run("run_b")],
        ):
            with mock.patch(
                "backend.app.ragas_eval.storage.load_trace",
                side_effect=fake_load_trace,
            ):
                samples, skipped = ragas_eval.collect_samples(limit=1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].run_id, "run_a")
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
                "backend.app.ragas_eval.storage.list_runs", return_value=[]
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
        error_trace = _trace(run_id="run_err", terminated_by="error", events=[])
        with mock.patch(
            "backend.app.ragas_eval.storage.list_runs",
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
        sqlite_trace = _trace(run_id="run_a")
        dumped_trace = _trace(run_id="run_b")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp)
            (trace_dir / "run_b.json").write_text(
                dumped_trace.model_dump_json(indent=2), encoding="utf-8"
            )

            def fake_load_trace(run_id: str):
                return sqlite_trace if run_id == "run_a" else None

            with mock.patch(
                "backend.app.ragas_eval.storage.list_runs",
                return_value=[_fake_run("run_a")],
            ):
                with mock.patch(
                    "backend.app.ragas_eval.storage.load_trace",
                    side_effect=fake_load_trace,
                ):
                    samples, skipped = ragas_eval.collect_samples(
                        trace_dir=trace_dir
                    )
        self.assertEqual({s.run_id for s in samples}, {"run_a", "run_b"})
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
                _trace(run_id="run_legacy").model_dump_json(indent=2),
                encoding="utf-8",
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_runs", return_value=[]
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
        sqlite_trace = _trace(run_id="run_gt")
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp)
            gt_dir = data_root / "runs" / "run_gt"
            gt_dir.mkdir(parents=True)
            (gt_dir / "ground_truth.json").write_text(
                json.dumps({"failure_type": "missed_constraint"}), encoding="utf-8"
            )
            with mock.patch(
                "backend.app.ragas_eval.storage.list_runs",
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


class BuildReportStubFallbackTests(unittest.TestCase):
    """build_report continues to fall back to stub when OPENAI_API_KEY
    is absent — A6.1 must not silently flip the report to real mode."""

    def test_build_report_force_stub_without_openai_key(self) -> None:
        sample = _sample()
        with mock.patch.dict(os.environ, {}, clear=True):
            report = ragas_eval.build_report([sample], ragas_eval.SkippedCounts())
        self.assertEqual(report.ragas_mode, "stub")
        self.assertEqual(report.fallback_reason, "OPENAI_API_KEY is not set")


if __name__ == "__main__":
    unittest.main()
