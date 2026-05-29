from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import agent_eval
from backend.app.schemas import AgentTrace, AgentTraceEvent


class AgentEvalHarnessTests(unittest.TestCase):
    def test_filter_golden_set_removes_failure_memory_source_overlap(self) -> None:
        golden = [
            agent_eval.GoldenSample(
                run_id="run_overlap",
                category="site",
                outcome="failed",
                failure_types=["missed_constraint"],
                failure_step=3,
                notes="",
            ),
            agent_eval.GoldenSample(
                run_id="run_clean",
                category="site",
                outcome="success",
                failure_types=[],
                failure_step=None,
                notes="",
            ),
        ]

        filtered, summary = agent_eval.filter_golden_set_for_failure_memory_overlap(
            golden,
            failure_memory_source_run_ids={"run_overlap"},
        )

        self.assertEqual([sample.run_id for sample in filtered], ["run_clean"])
        self.assertEqual(summary.original_n, 2)
        self.assertEqual(summary.evaluated_n, 1)
        self.assertEqual(summary.failure_memory_overlap_n, 1)
        self.assertEqual(summary.failure_memory_overlap_run_ids, ["run_overlap"])

    def test_report_records_failure_memory_overlap_filter_count(self) -> None:
        summary = agent_eval.GoldenSetFilterSummary(
            original_n=5,
            evaluated_n=3,
            failure_memory_overlap_n=2,
            failure_memory_overlap_run_ids=["run_a", "run_b"],
        )
        report = agent_eval.build_report(
            [],
            agent_eval.SkippedCounts(),
            agent_mode="mock",
            label_baselines={},
            golden_set_filter=summary,
        )

        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = agent_eval.write_report(report, Path(tmp))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")

        self.assertEqual(payload["golden_set_filter"]["original_n"], 5)
        self.assertEqual(payload["golden_set_filter"]["failure_memory_overlap_n"], 2)
        self.assertIn("excluded_failure_memory_overlap=2", markdown)

    def test_compute_label_baselines_returns_binary_baselines(self) -> None:
        """Binary baselines are now the primary signal; verify they're computed
        and that the success/failed counts feed the majority class correctly."""
        golden = [
            agent_eval.GoldenSample(
                run_id=f"success_{i}", category="x", outcome="success",
                failure_types=[], failure_step=None, notes="",
            )
            for i in range(5)
        ] + [
            agent_eval.GoldenSample(
                run_id=f"failed_{i}", category="x", outcome="failed",
                failure_types=["early_terminated"], failure_step=2, notes="",
            )
            for i in range(3)
        ]

        bl = agent_eval.compute_label_baselines(golden)

        # 5 success vs 3 failed → majority = success, accuracy 5/8.
        self.assertEqual(bl["binary_majority_class"], "success")
        self.assertEqual(bl["binary_majority_baseline_accuracy"], 5 / 8)
        self.assertEqual(bl["binary_random_baseline_accuracy"], 0.5)
        self.assertEqual(bl["n_binary_samples"], 8)
        self.assertEqual(bl["binary_success_n"], 5)
        self.assertEqual(bl["binary_failed_n"], 3)
        # Legacy failure_type baselines still present (advisory only).
        self.assertEqual(bl["majority_class"], "early_terminated")
        self.assertEqual(bl["n_failed_samples"], 3)

    def test_report_demotes_failure_type_to_advisory(self) -> None:
        """Markdown headline must be binary verdict; failure_type lives in
        an advisory section. Locks in the #3 reporting policy."""
        report = agent_eval.build_report(
            [],
            agent_eval.SkippedCounts(),
            agent_mode="mock",
            label_baselines={
                "binary_majority_class": "success",
                "binary_majority_baseline_accuracy": 0.6,
                "binary_random_baseline_accuracy": 0.5,
                "n_binary_samples": 10,
                "binary_success_n": 6,
                "binary_failed_n": 4,
                "majority_class": "early_terminated",
                "majority_baseline_accuracy": 0.5,
                "random_baseline_expected_accuracy": 0.2,
                "n_failed_samples": 4,
                "vocabulary_size": 5,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = agent_eval.write_report(report, Path(tmp))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = md_path.read_text(encoding="utf-8")

        # JSON exposes the policy explicitly.
        self.assertEqual(payload["primary_metric"], "binary_verdict_accuracy")

        # Markdown: binary table is the headline (first metric section).
        binary_idx = markdown.find("## Binary verdict accuracy vs. baselines")
        advisory_idx = markdown.find("## Advisory: failure-type classification")
        self.assertNotEqual(binary_idx, -1, "binary headline section missing")
        self.assertNotEqual(advisory_idx, -1, "advisory section missing")
        self.assertLess(binary_idx, advisory_idx, "binary must come before advisory")

        # Caveats explicitly demote failure_type.
        self.assertIn("Primary metric is `binary_verdict_accuracy`", markdown)
        self.assertIn("advisory", markdown.lower())


class TraceDumpTests(unittest.TestCase):
    """Phase 8 A2 — per-sample trace dump path used by the judge (A3) and
    the real RAGAS run (A6)."""

    def _make_trace(self, run_id: str) -> AgentTrace:
        """A minimal AgentTrace shaped enough that judge.py would consume it.

        The trace carries a propose_eval_case tool_call so the
        ``evidence`` field round-trip is covered (that's what the judge
        clauses 1–5 read against), plus monotonic seq/turn so the
        AgentTrace invariants pass on validation.
        """
        events = [
            AgentTraceEvent(seq=0, turn=0, type="agent_message", message="thinking"),
            AgentTraceEvent(
                seq=1,
                turn=0,
                type="tool_call",
                name="propose_eval_case",
                args={
                    "run_id": run_id,
                    "failure_step": 3,
                    "failure_type": "missed_constraint",
                    "expected_behavior": "satisfy constraint",
                    "actual_behavior": "did not satisfy constraint",
                    "evidence": [
                        {
                            "claim": "step 3 inspected",
                            "source": "step_detail_high",
                            "run_id": run_id,
                            "step_index": 3,
                        }
                    ],
                    "regression_rule": "verify constraint",
                    "retrieved_context_ids": ["fm_missed_constraint_001"],
                },
            ),
        ]
        return AgentTrace(
            run_id=run_id,
            user_intent="analyze_run",
            selected_step=None,
            tool_call_count=1,
            turn_count=1,
            terminated_by="propose_eval_case",
            events=events,
            model="mock",
            prompt_version="v5_constraint_verification",
            prompt_sha256="0" * 64,
            vlm_model="mock",
        )

    def test_dump_trace_writes_run_id_named_file(self) -> None:
        """_dump_trace creates {trace_dir}/{run_id}.json with the
        round-trippable AgentTrace payload."""
        trace = self._make_trace("run_abc")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            out = agent_eval._dump_trace(trace, trace_dir, "run_abc")

            self.assertIsNotNone(out)
            self.assertEqual(out, trace_dir / "run_abc.json")
            payload = json.loads(out.read_text(encoding="utf-8"))
            # AgentTrace round-trips through model_dump_json.
            self.assertEqual(payload["run_id"], "run_abc")
            self.assertEqual(payload["terminated_by"], "propose_eval_case")
            # Evidence chain is preserved (the field A3 judge reads from).
            propose_args = next(
                ev["args"] for ev in payload["events"]
                if ev["type"] == "tool_call" and ev["name"] == "propose_eval_case"
            )
            self.assertEqual(len(propose_args["evidence"]), 1)
            self.assertEqual(propose_args["evidence"][0]["source"], "step_detail_high")
            self.assertEqual(propose_args["evidence"][0]["step_index"], 3)
            # Prompt version stamp survives (A7 experiment-log needs it).
            self.assertEqual(payload["prompt_version"], "v5_constraint_verification")

    def test_dump_trace_creates_parent_dirs(self) -> None:
        """First-time dump should create the nested trace dir, including
        the eval/runs/<stamp>/traces case the default path uses."""
        trace = self._make_trace("run_nested")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "runs" / "2026-05-29T00-00-00Z" / "traces"
            self.assertFalse(trace_dir.exists())
            out = agent_eval._dump_trace(trace, trace_dir, "run_nested")
            self.assertIsNotNone(out)
            self.assertTrue(trace_dir.exists())
            self.assertTrue(out.exists())

    def test_dump_trace_swallows_oserror(self) -> None:
        """A flaky filesystem must not lose an otherwise-successful
        grading run — the wrapper logs and returns None, the caller
        continues."""
        trace = self._make_trace("run_oserror")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            # Simulate the OS rejecting the write (e.g. disk full).
            with patch(
                "pathlib.Path.write_text",
                side_effect=OSError("simulated ENOSPC"),
            ):
                result = agent_eval._dump_trace(trace, trace_dir, "run_oserror")
            self.assertIsNone(result)
            # The wrapper still created the dir before the write failed —
            # that's fine; what matters is no exception escapes.
            self.assertFalse((trace_dir / "run_oserror.json").exists())

    def test_collect_graded_samples_dumps_when_trace_dir_set(self) -> None:
        """Integration: collect_graded_samples writes one trace JSON per
        gradeable sample when ``trace_dir`` is passed. Verifies the A2
        wiring without depending on a real LLM — uses a stub
        ``_run_agent`` so the test stays deterministic."""
        sample = agent_eval.GoldenSample(
            run_id="run_dump_a",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )
        trace = self._make_trace("run_dump_a")

        # Stub storage.load_run so the sample is "importable" without a real
        # SQLite row.
        class _FakeRun:
            steps = [None] * 5

        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            with (
                patch("backend.app.agent_eval.storage.load_run", return_value=_FakeRun()),
                patch("backend.app.agent_eval._run_agent", return_value=trace),
            ):
                graded, skipped = agent_eval.collect_graded_samples(
                    [sample],
                    force_mock=True,
                    trace_dir=trace_dir,
                )
            self.assertEqual(len(graded), 1)
            self.assertEqual(skipped.not_importable, 0)
            self.assertTrue((trace_dir / "run_dump_a.json").exists())

    def test_collect_graded_samples_no_dump_when_trace_dir_none(self) -> None:
        """The opt-out path: ``trace_dir=None`` produces no files (mock
        mode default)."""
        sample = agent_eval.GoldenSample(
            run_id="run_no_dump",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )
        trace = self._make_trace("run_no_dump")

        class _FakeRun:
            steps = [None] * 5

        with tempfile.TemporaryDirectory() as tmp:
            # No trace dir is created or populated when trace_dir=None.
            with (
                patch("backend.app.agent_eval.storage.load_run", return_value=_FakeRun()),
                patch("backend.app.agent_eval._run_agent", return_value=trace),
            ):
                graded, _ = agent_eval.collect_graded_samples(
                    [sample],
                    force_mock=True,
                    trace_dir=None,
                )
            self.assertEqual(len(graded), 1)
            # The temp dir was never touched by trace dumping.
            self.assertFalse(any(Path(tmp).rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
