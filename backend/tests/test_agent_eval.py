from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.app import agent_eval
from backend.app.schemas import AgentTrace, AgentTraceEvent
from eval.judge import JudgeConfig, JudgeProviderError, StandaloneJudgeResult


class AgentEvalHarnessTests(unittest.TestCase):
    def test_filter_golden_set_removes_failure_memory_source_overlap(self) -> None:
        golden = [
            agent_eval.GoldenSample(
                trajectory_id="run_overlap",
                category="site",
                outcome="failed",
                failure_types=["missed_constraint"],
                failure_step=3,
                notes="",
            ),
            agent_eval.GoldenSample(
                trajectory_id="run_clean",
                category="site",
                outcome="success",
                failure_types=[],
                failure_step=None,
                notes="",
            ),
        ]

        filtered, summary = agent_eval.filter_golden_set_for_failure_memory_overlap(
            golden,
            failure_memory_source_trajectory_ids={"run_overlap"},
        )

        self.assertEqual([sample.trajectory_id for sample in filtered], ["run_clean"])
        self.assertEqual(summary.original_n, 2)
        self.assertEqual(summary.evaluated_n, 1)
        self.assertEqual(summary.failure_memory_overlap_n, 1)
        self.assertEqual(summary.failure_memory_overlap_trajectory_ids, ["run_overlap"])

    def test_report_records_failure_memory_overlap_filter_count(self) -> None:
        summary = agent_eval.GoldenSetFilterSummary(
            original_n=5,
            evaluated_n=3,
            failure_memory_overlap_n=2,
            failure_memory_overlap_trajectory_ids=["run_a", "run_b"],
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
                trajectory_id=f"success_{i}", category="x", outcome="success",
                failure_types=[], failure_step=None, notes="",
            )
            for i in range(5)
        ] + [
            agent_eval.GoldenSample(
                trajectory_id=f"failed_{i}", category="x", outcome="failed",
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

    def _make_trace(self, trajectory_id: str) -> AgentTrace:
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
                    "trajectory_id": trajectory_id,
                    "failure_step": 3,
                    "failure_type": "missed_constraint",
                    "expected_behavior": "satisfy constraint",
                    "actual_behavior": "did not satisfy constraint",
                    "evidence": [
                        {
                            "claim": "step 3 inspected",
                            "source": "step_detail_high",
                            "trajectory_id": trajectory_id,
                            "step_index": 3,
                        }
                    ],
                    "regression_rule": "verify constraint",
                    "retrieved_context_ids": ["fm_missed_constraint_001"],
                },
            ),
        ]
        return AgentTrace(
            trajectory_id=trajectory_id,
            user_intent="analyze_trajectory",
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

    def test_dump_trace_writes_trajectory_id_named_file(self) -> None:
        """_dump_trace creates {trace_dir}/{trajectory_id}.json with the
        round-trippable AgentTrace payload."""
        trace = self._make_trace("run_abc")
        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            out = agent_eval._dump_trace(trace, trace_dir, "run_abc")

            self.assertIsNotNone(out)
            self.assertEqual(out, trace_dir / "run_abc.json")
            payload = json.loads(out.read_text(encoding="utf-8"))
            # AgentTrace round-trips through model_dump_json.
            self.assertEqual(payload["trajectory_id"], "run_abc")
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
            trajectory_id="run_dump_a",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )
        trace = self._make_trace("run_dump_a")

        # Stub storage.load_trajectory so the sample is "importable" without a real
        # SQLite row.
        class _FakeRun:
            steps = [None] * 5

        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            with (
                patch("backend.app.agent_eval.storage.load_trajectory", return_value=_FakeRun()),
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
            trajectory_id="run_no_dump",
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
                patch("backend.app.agent_eval.storage.load_trajectory", return_value=_FakeRun()),
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

    def test_collect_graded_samples_retries_retryable_error(self) -> None:
        """429 / transient API errors are retried at the sample level."""
        sample = agent_eval.GoldenSample(
            trajectory_id="run_retry",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )
        trace = self._make_trace("run_retry")

        class _FakeRun:
            steps = [None] * 5

        delays: list[float] = []
        run_agent = MagicMock(side_effect=[RuntimeError("429 rate limit"), trace])
        with (
            patch("backend.app.agent_eval.storage.load_trajectory", return_value=_FakeRun()),
            patch("backend.app.agent_eval._run_agent", run_agent),
        ):
            graded, skipped = agent_eval.collect_graded_samples(
                [sample],
                force_mock=False,
                max_retries=2,
                retry_base_s=1.0,
                retry_max_s=10.0,
                sleep_fn=delays.append,
            )

        self.assertEqual(len(graded), 1)
        self.assertEqual(skipped.agent_error, 0)
        self.assertEqual(run_agent.call_count, 2)
        self.assertEqual(delays, [1.0])

    def test_collect_graded_samples_does_not_retry_non_retryable_error(self) -> None:
        """Non-transient failures keep the old behavior: mark agent_error and continue."""
        sample = agent_eval.GoldenSample(
            trajectory_id="run_non_retry",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )

        class _FakeRun:
            steps = [None] * 5

        run_agent = MagicMock(side_effect=ValueError("bad eval case schema"))
        with (
            patch("backend.app.agent_eval.storage.load_trajectory", return_value=_FakeRun()),
            patch("backend.app.agent_eval._run_agent", run_agent),
        ):
            graded, skipped = agent_eval.collect_graded_samples(
                [sample],
                force_mock=False,
                max_retries=3,
                retry_base_s=1.0,
                retry_max_s=10.0,
                sleep_fn=lambda _delay: None,
            )

        self.assertEqual(graded, [])
        self.assertEqual(skipped.agent_error, 1)
        self.assertEqual(run_agent.call_count, 1)

    def test_collect_graded_samples_resumes_existing_trace(self) -> None:
        """Existing trace_dir/{trajectory_id}.json is graded without calling the agent."""
        sample = agent_eval.GoldenSample(
            trajectory_id="run_resume",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )
        trace = self._make_trace("run_resume")
        trace.runtime_ms = 1234

        class _FakeRun:
            steps = [None] * 5

        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            trace_dir.mkdir()
            (trace_dir / "run_resume.json").write_text(
                trace.model_dump_json(),
                encoding="utf-8",
            )
            run_agent = MagicMock()
            with (
                patch.dict(
                    os.environ,
                    {"TRAJECTA_PROMPT_VERSION": "v5_constraint_verification"},
                ),
                patch("backend.app.agent_eval.storage.load_trajectory", return_value=_FakeRun()),
                patch("backend.app.agent_eval._run_agent", run_agent),
            ):
                graded, skipped = agent_eval.collect_graded_samples(
                    [sample],
                    force_mock=False,
                    trace_dir=trace_dir,
                )

        self.assertEqual(len(graded), 1)
        self.assertEqual(graded[0].latency_s, 1.234)
        self.assertEqual(skipped.agent_error, 0)
        run_agent.assert_not_called()

    def test_collect_graded_samples_rejects_prompt_mismatched_trace(self) -> None:
        """Resume refuses traces from another TRAJECTA_PROMPT_VERSION."""
        sample = agent_eval.GoldenSample(
            trajectory_id="run_prompt_mismatch",
            category="x",
            outcome="success",
            failure_types=[],
            failure_step=None,
            notes="",
        )
        trace = self._make_trace("run_prompt_mismatch")

        class _FakeRun:
            steps = [None] * 5

        with tempfile.TemporaryDirectory() as tmp:
            trace_dir = Path(tmp) / "traces"
            trace_dir.mkdir()
            (trace_dir / "run_prompt_mismatch.json").write_text(
                trace.model_dump_json(),
                encoding="utf-8",
            )
            run_agent = MagicMock()
            with (
                patch.dict(os.environ, {"TRAJECTA_PROMPT_VERSION": "v3_balanced_rubric"}),
                patch("backend.app.agent_eval.storage.load_trajectory", return_value=_FakeRun()),
                patch("backend.app.agent_eval._run_agent", run_agent),
            ):
                with self.assertRaises(ValueError):
                    agent_eval.collect_graded_samples(
                        [sample],
                        force_mock=False,
                        trace_dir=trace_dir,
                    )

        run_agent.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 8 A3.5 — `agent_eval --judge` post-step
#
# These tests cover the glue layer that fans the env-configured judge
# slots across the just-written eval artefacts via the A3.4
# ``run_standalone_judge`` seam. They never call a real Gemini/OpenAI
# client; the post-step accepts ``judge_callable`` / ``env`` /
# ``prompts_root`` injections so the path stays deterministic and the
# A4.1 provider wiring stays out of scope.


def _make_judge_result(
    *,
    slot: str = "A",
    model: str = "judge-a",
    prompt_version: str = "v_test",
    trajectory_id: str = "run_x",
) -> StandaloneJudgeResult:
    """A minimal StandaloneJudgeResult used by tests that mock
    ``run_standalone_judge`` — only ``graded_trajectory_ids`` / ``skipped`` are
    actually read by the post-step's stderr summary."""
    from eval.judge import JudgeCaseReport, JudgeReport

    judge = JudgeConfig(slot=slot, model=model, prompt_version=prompt_version)  # type: ignore[arg-type]
    report = JudgeReport(
        judge=judge,
        prompt_sha256="0" * 64,
        cases=[
            JudgeCaseReport(
                trajectory_id=trajectory_id, verdict="acceptable", rationale="ok", assertions=[]
            )
        ],
    )
    return StandaloneJudgeResult(
        report=report,
        json_path=Path("/tmp/judge_report.json"),
        md_path=Path("/tmp/judge_report.md"),
        graded_trajectory_ids=[trajectory_id],
        skipped={},
    )


class JudgeFlagParseTests(unittest.TestCase):
    """The CLI flag plus the early --mock+--judge rejection are the only
    bits of A3.5 that live inside the argparse layer; everything else
    delegates to :func:`agent_eval._run_judge_post_step`."""

    def test_parser_accepts_judge_flag(self) -> None:
        """The --judge flag must be a recognized CLI option, default off.
        Argparse is opaque to grep, so locking it in via test catches a
        future refactor that accidentally drops the flag."""
        # main() builds the parser inline; capture by patching parse_args.
        captured: dict[str, object] = {}

        def fake_parse(self_, argv):
            ns = unittest.mock.Mock()
            ns.judge = True
            ns.mock = True  # short-circuit out of main() right after the check
            captured["ns"] = ns
            return ns

        # Bypass argparse altogether to focus on the flag wiring: simulate
        # `--judge --mock` and confirm main() rejects with code 2.
        with patch("argparse.ArgumentParser.parse_args", fake_parse):
            # main() inspects args.judge before any heavy work; the
            # rejection path returns 2 without touching storage/eval.
            rc = agent_eval.main(["--judge", "--mock"])
        self.assertEqual(rc, 2)
        self.assertTrue(captured["ns"].judge)

    def test_judge_with_mock_short_circuits_with_clear_error(self) -> None:
        """`--judge` + `--mock` is a misuse: mock mode never writes the
        production report the judge would grade. main() must refuse
        loudly before any eval work happens."""
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            rc = agent_eval.main(["--judge", "--mock"])
        self.assertEqual(rc, 2)
        msg = stderr.getvalue()
        self.assertIn("--judge", msg)
        self.assertIn("--mock", msg)


class RunJudgePostStepTests(unittest.TestCase):
    """Tests for ``_run_judge_post_step`` — the glue between agent_eval
    and the A3.4 standalone runner. Inputs are constructed fresh per
    test in tmp_path so the post-step exercises the real
    ``run_standalone_judge`` path end-to-end with mocked
    ``judge_callable`` / ``env`` / ``prompts_root`` injections."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.trajectory_id = "run_x"
        # Minimal agent_report.json — the standalone runner only reads
        # samples[].trajectory_id from it.
        self.report_path = self.tmp / "agent_report.json"
        self.report_path.write_text(
            json.dumps({"samples": [{"trajectory_id": self.trajectory_id}]}),
            encoding="utf-8",
        )
        # Trace dump matching the report's sole sample.
        self.trace_dir = self.tmp / "traces"
        self.trace_dir.mkdir()
        trace = AgentTrace(
            trajectory_id=self.trajectory_id,
            user_intent="analyze_trajectory",
            selected_step=None,
            tool_call_count=1,
            turn_count=1,
            terminated_by="propose_eval_case",
            events=[
                AgentTraceEvent(
                    seq=0,
                    turn=0,
                    type="tool_call",
                    name="propose_eval_case",
                    args={
                        "trajectory_id": self.trajectory_id,
                        "failure_step": 3,
                        "failure_type": "missed_constraint",
                        "expected_behavior": "x",
                        "actual_behavior": "y",
                        "evidence": [],
                        "regression_rule": "z",
                        "retrieved_context_ids": [],
                    },
                )
            ],
            model="mock",
            prompt_version="v5",
            prompt_sha256="0" * 64,
            vlm_model="mock",
        )
        (self.trace_dir / f"{self.trajectory_id}.json").write_text(
            trace.model_dump_json(indent=2), encoding="utf-8"
        )
        # Golden JSONL covering the trajectory_id.
        self.golden_path = self.tmp / "golden.jsonl"
        self.golden_path.write_text(
            json.dumps(
                {
                    "input": {"trajectory_id": self.trajectory_id, "intent": "analyze_trajectory"},
                    "expected_facts": [
                        {"field": "outcome", "op": "eq", "value": "failed"},
                        {
                            "field": "failure_type",
                            "op": "in",
                            "value": ["missed_constraint"],
                        },
                    ],
                    "forbidden_facts": [
                        {"field": "outcome", "op": "eq", "value": "success"}
                    ],
                    "tags": ["site"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # Stand-in prompts/judge/<v>/prompt.md tree so the test does not
        # depend on the committed bundle's exact bytes.
        self.prompts_root = self.tmp / "judge_prompts"
        (self.prompts_root / "v_test").mkdir(parents=True)
        (self.prompts_root / "v_test" / "prompt.md").write_text(
            "rubric", encoding="utf-8"
        )
        # The archive dir that production main() would have computed.
        self.archive_dir = self.tmp / "runs" / "2026-05-29T00-00-00Z"
        self.archive_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _accept_callable(self):
        def _fake(prompt: str, payload: dict[str, object]) -> str:
            return json.dumps(
                {
                    "verdict": "acceptable",
                    "rationale": "mock",
                    "assertions": [
                        {
                            "name": "verdict_alignment",
                            "status": "pass",
                            "rationale": "ok",
                        }
                    ],
                }
            )

        return _fake

    def test_missing_trace_dir_returns_2(self) -> None:
        """--judge requires per-sample traces. main() also blocks this
        path (mock mode is the only way to reach trace_dir=None and we
        reject --mock+--judge earlier), but the helper still has to
        defend itself for callers that bypass main."""
        rc = agent_eval._run_judge_post_step(
            report_path=self.report_path,
            trace_dir=None,
            archive_dir=self.archive_dir,
            golden_path=self.golden_path,
            judge_callable=self._accept_callable(),
            env={"TRAJECTA_JUDGE_A_MODEL": "m", "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test"},
            prompts_root=self.prompts_root,
        )
        self.assertEqual(rc, 2)

    def test_no_env_config_returns_2(self) -> None:
        """--judge with neither slot configured is an operator wiring
        bug, not a partial success — the post-step must refuse rather
        than producing an empty judge directory."""
        rc = agent_eval._run_judge_post_step(
            report_path=self.report_path,
            trace_dir=self.trace_dir,
            archive_dir=self.archive_dir,
            golden_path=self.golden_path,
            judge_callable=self._accept_callable(),
            env={},
            prompts_root=self.prompts_root,
        )
        self.assertEqual(rc, 2)

    def test_partial_env_config_for_one_slot_is_not_configured(self) -> None:
        """A slot whose MODEL is set but PROMPT_VERSION is missing (or
        vice versa) is not a configured slot — judge_config_from_env
        already enforces that. The post-step must surface "no slots
        configured" rather than silently grading on a half-configured
        slot."""
        rc = agent_eval._run_judge_post_step(
            report_path=self.report_path,
            trace_dir=self.trace_dir,
            archive_dir=self.archive_dir,
            golden_path=self.golden_path,
            judge_callable=self._accept_callable(),
            env={"TRAJECTA_JUDGE_A_MODEL": "m"},  # missing PROMPT_VERSION
            prompts_root=self.prompts_root,
        )
        self.assertEqual(rc, 2)

    def test_runs_only_slot_A_when_only_slot_A_configured(self) -> None:
        """End-to-end with a real run_standalone_judge call: slot A in
        env, slot B missing → exactly one judge_report.json under
        <archive>/judge/A/. The other slot's directory must not exist."""
        rc = agent_eval._run_judge_post_step(
            report_path=self.report_path,
            trace_dir=self.trace_dir,
            archive_dir=self.archive_dir,
            golden_path=self.golden_path,
            judge_callable=self._accept_callable(),
            env={
                "TRAJECTA_JUDGE_A_MODEL": "gemini-flash-mock",
                "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
            },
            prompts_root=self.prompts_root,
        )
        self.assertEqual(rc, 0)
        a_json = self.archive_dir / "judge" / "A" / "judge_report.json"
        b_dir = self.archive_dir / "judge" / "B"
        agreement_json = self.archive_dir / "judge" / "judge_agreement_report.json"
        self.assertTrue(a_json.exists())
        self.assertTrue(a_json.with_suffix(".md").exists())
        self.assertFalse(b_dir.exists())
        self.assertFalse(agreement_json.exists())
        # The report carries the slot's traceability triple end-to-end.
        data = json.loads(a_json.read_text(encoding="utf-8"))
        self.assertEqual(data["judge"]["slot"], "A")
        self.assertEqual(data["judge"]["model"], "gemini-flash-mock")
        self.assertEqual(data["judge"]["prompt_version"], "v_test")
        self.assertEqual(data["sample_size"], 1)

    def test_runs_both_slots_when_both_configured(self) -> None:
        """Slot A and slot B both configured → two judge stanzas land
        on disk. The post-step calls them in deterministic A-then-B
        order so the operator can reason about runtime cost."""
        call_log: list[str] = []

        def fake(prompt: str, payload: dict[str, object]) -> str:
            call_log.append("invoked")
            return json.dumps(
                {
                    "verdict": "acceptable",
                    "rationale": "mock",
                    "assertions": [
                        {
                            "name": "verdict_alignment",
                            "status": "pass",
                            "rationale": "ok",
                        }
                    ],
                }
            )

        rc = agent_eval._run_judge_post_step(
            report_path=self.report_path,
            trace_dir=self.trace_dir,
            archive_dir=self.archive_dir,
            golden_path=self.golden_path,
            judge_callable=fake,
            env={
                "TRAJECTA_JUDGE_A_MODEL": "gemini-mock",
                "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
                "TRAJECTA_JUDGE_B_MODEL": "openai-mock",
                "TRAJECTA_JUDGE_B_PROMPT_VERSION": "v_test",
            },
            prompts_root=self.prompts_root,
        )
        self.assertEqual(rc, 0)
        a_json = self.archive_dir / "judge" / "A" / "judge_report.json"
        b_json = self.archive_dir / "judge" / "B" / "judge_report.json"
        agreement_json = self.archive_dir / "judge" / "judge_agreement_report.json"
        self.assertTrue(a_json.exists())
        self.assertTrue(b_json.exists())
        self.assertTrue(agreement_json.exists())
        self.assertTrue(agreement_json.with_suffix(".md").exists())
        # One judge call per slot (one gradeable sample × two slots).
        self.assertEqual(len(call_log), 2)
        a_data = json.loads(a_json.read_text(encoding="utf-8"))
        b_data = json.loads(b_json.read_text(encoding="utf-8"))
        agreement_data = json.loads(agreement_json.read_text(encoding="utf-8"))
        self.assertEqual(a_data["judge"]["model"], "gemini-mock")
        self.assertEqual(b_data["judge"]["model"], "openai-mock")
        self.assertEqual(agreement_data["sample_size"], 1)
        self.assertEqual(agreement_data["kappa_llm_llm"], 1.0)

    def test_no_api_key_marks_slot_as_failed(self) -> None:
        """A4.1: when MODEL+PROMPT_VERSION are set but the slot's
        ``API_KEY`` env is missing, the default callable raises
        ``JudgeProviderError`` at construction time. The post-step
        treats that as a per-slot ``failed`` entry and returns exit
        code 1 (all configured slots failed)."""
        # Scrub OPENAI_API_KEY so the slot-B fallback also can't kick in
        # for tests that exercise slot A — the env injection isolates
        # everything to ``env=``.
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            rc = agent_eval._run_judge_post_step(
                report_path=self.report_path,
                trace_dir=self.trace_dir,
                archive_dir=self.archive_dir,
                golden_path=self.golden_path,
                judge_callable=None,  # default callable; resolver needs API key in env
                env={
                    "TRAJECTA_JUDGE_A_MODEL": "gemini-mock",
                    "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
                },
                prompts_root=self.prompts_root,
            )
        self.assertEqual(rc, 1)
        msg = stderr.getvalue()
        self.assertIn("failed", msg)
        self.assertIn("TRAJECTA_JUDGE_A_API_KEY", msg)
        # The judge subdirectory must not contain a partial report for
        # the failed slot — write_judge_report only fires on success.
        self.assertFalse(
            (self.archive_dir / "judge" / "A" / "judge_report.json").exists()
        )

    def test_one_slot_succeeds_one_slot_fails_returns_0(self) -> None:
        """Mixed outcome: slot A configured with a working callable
        produces a report; slot B's provider call fails with
        ``JudgeProviderError`` (e.g. missing API key, 4xx, transport
        error). At least one slot produced a real report so the
        post-step returns 0 — a partial success, not a failure."""
        from eval.judge import JudgeConfig as _JudgeConfig

        def selective_callable(prompt: str, payload: dict[str, object]) -> str:
            return json.dumps(
                {
                    "verdict": "acceptable",
                    "rationale": "mock",
                    "assertions": [
                        {
                            "name": "verdict_alignment",
                            "status": "pass",
                            "rationale": "ok",
                        }
                    ],
                }
            )

        # Patch run_standalone_judge to raise JudgeProviderError for
        # slot B but defer to the real runner for slot A.
        real = agent_eval.run_standalone_judge

        def fake_runner(*, config: _JudgeConfig, **kwargs):
            if config.slot == "B":
                raise JudgeProviderError(
                    "judge slot 'B' provider call failed (simulated)"
                )
            return real(config=config, **kwargs)

        with patch.object(agent_eval, "run_standalone_judge", side_effect=fake_runner):
            rc = agent_eval._run_judge_post_step(
                report_path=self.report_path,
                trace_dir=self.trace_dir,
                archive_dir=self.archive_dir,
                golden_path=self.golden_path,
                judge_callable=selective_callable,
                env={
                    "TRAJECTA_JUDGE_A_MODEL": "m",
                    "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
                    "TRAJECTA_JUDGE_B_MODEL": "n",
                    "TRAJECTA_JUDGE_B_PROMPT_VERSION": "v_test",
                },
                prompts_root=self.prompts_root,
            )
        self.assertEqual(rc, 0)
        self.assertTrue(
            (self.archive_dir / "judge" / "A" / "judge_report.json").exists()
        )
        # Slot B's directory does not exist because the writer never ran.
        self.assertFalse(
            (self.archive_dir / "judge" / "B" / "judge_report.json").exists()
        )
        self.assertFalse(
            (self.archive_dir / "judge" / "judge_agreement_report.json").exists()
        )

    def test_agreement_mismatch_returns_1_without_writing_report(self) -> None:
        """If both slots run but grade different trajectory_ids, κ is invalid.
        The post-step should surface that as a judge failure instead of
        silently writing a misleading agreement report."""

        def fake_runner(*, config: JudgeConfig, **kwargs):
            if config.slot == "A":
                return _make_judge_result(slot="A", model="a", trajectory_id="run_x")
            return _make_judge_result(slot="B", model="b", trajectory_id="run_y")

        stderr = io.StringIO()
        with (
            patch.object(agent_eval, "run_standalone_judge", side_effect=fake_runner),
            patch.object(sys, "stderr", stderr),
        ):
            rc = agent_eval._run_judge_post_step(
                report_path=self.report_path,
                trace_dir=self.trace_dir,
                archive_dir=self.archive_dir,
                golden_path=self.golden_path,
                judge_callable=self._accept_callable(),
                env={
                    "TRAJECTA_JUDGE_A_MODEL": "m",
                    "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
                    "TRAJECTA_JUDGE_B_MODEL": "n",
                    "TRAJECTA_JUDGE_B_PROMPT_VERSION": "v_test",
                },
                prompts_root=self.prompts_root,
            )

        self.assertEqual(rc, 1)
        self.assertIn("Judge agreement: failed", stderr.getvalue())
        self.assertFalse(
            (self.archive_dir / "judge" / "judge_agreement_report.json").exists()
        )

    def test_default_callable_with_env_api_key_runs_end_to_end(self) -> None:
        """A4.1 sanity: when the slot's API key env is present the
        post-step resolves the default callable, which builds a real
        ``openai.OpenAI`` client. With ``openai.OpenAI`` patched to a
        fake constructor, the post-step produces a judge report
        without any network call — same end-state as injecting a
        callable explicitly, but exercising the env → resolver →
        provider wiring."""
        import openai

        fake_response = json.dumps(
            {
                "verdict": "acceptable",
                "rationale": "mock",
                "assertions": [
                    {
                        "name": "verdict_alignment",
                        "status": "pass",
                        "rationale": "ok",
                    }
                ],
            }
        )

        class _FakeMessage:
            content = fake_response

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeCompletion:
            choices = [_FakeChoice()]

        class _FakeChat:
            class completions:  # noqa: N801 — mimic openai's nested shape
                @staticmethod
                def create(**kwargs):
                    return _FakeCompletion()

        class _FakeOpenAIClient:
            def __init__(self, **kwargs):
                self.chat = _FakeChat()

        with patch.object(openai, "OpenAI", _FakeOpenAIClient):
            rc = agent_eval._run_judge_post_step(
                report_path=self.report_path,
                trace_dir=self.trace_dir,
                archive_dir=self.archive_dir,
                golden_path=self.golden_path,
                judge_callable=None,  # forces _default_judge_callable
                env={
                    "TRAJECTA_JUDGE_A_MODEL": "gemini-mock",
                    "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
                    "TRAJECTA_JUDGE_A_API_KEY": "k",
                },
                prompts_root=self.prompts_root,
            )
        self.assertEqual(rc, 0)
        a_json = self.archive_dir / "judge" / "A" / "judge_report.json"
        self.assertTrue(a_json.exists())
        data = json.loads(a_json.read_text(encoding="utf-8"))
        self.assertEqual(data["judge"]["model"], "gemini-mock")
        self.assertEqual(data["sample_size"], 1)

    def test_writes_into_archive_judge_slot_subdir(self) -> None:
        """The output layout is fixed by docs/phase8_s18_alignment.md:
        artefacts land under <archive_dir>/judge/<slot>/ so reruns are
        self-contained and a future κ rollup can fan two slots into a
        single directory without colliding."""
        agent_eval._run_judge_post_step(
            report_path=self.report_path,
            trace_dir=self.trace_dir,
            archive_dir=self.archive_dir,
            golden_path=self.golden_path,
            judge_callable=self._accept_callable(),
            env={
                "TRAJECTA_JUDGE_A_MODEL": "m",
                "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_test",
            },
            prompts_root=self.prompts_root,
        )
        expected = self.archive_dir / "judge" / "A" / "judge_report.json"
        self.assertTrue(expected.exists())
        # Sibling markdown lives in the same directory.
        self.assertTrue(expected.with_suffix(".md").exists())


class MainJudgeWiringTests(unittest.TestCase):
    """Lightweight integration tests for ``main(--judge)``: confirm the
    flag reaches ``_run_judge_post_step`` and that the helper's exit
    code propagates out of ``main``. The post-step's behaviour itself is
    covered by RunJudgePostStepTests above; here we only exercise the
    wiring."""

    def setUp(self) -> None:
        # main() unconditionally sets TRAJECTA_EVAL_MODE=1 when unset,
        # and the patch.dict scope below would otherwise let that
        # mutation persist into later tests (test_rag's redaction
        # assertions in particular). Snapshot+restore the full env so
        # nothing leaks between tests in this class.
        self._env_patch = patch.dict(os.environ, os.environ.copy(), clear=True)
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_main_judge_propagates_post_step_exit_code(self) -> None:
        """When --judge is set, main() must return whatever
        ``_run_judge_post_step`` returns — not the eval's own 0. A
        regression that hardcodes ``return 0`` after the judge call
        would silently mask judge failures."""
        # Bypass the full eval pipeline; only the last segment of main()
        # is under test. We stub argparse to skip the heavy work and
        # drive straight to the judge invocation by re-implementing the
        # tail of main() through patches.
        with (
            patch.object(agent_eval, "_run_judge_post_step", return_value=7) as mock_post,
            patch.object(agent_eval, "load_golden_set", return_value=[]),
            patch.object(
                agent_eval,
                "filter_golden_set_for_failure_memory_overlap",
                return_value=([], agent_eval.GoldenSetFilterSummary()),
            ),
            patch.object(agent_eval.rag, "hydrate_all"),
            patch.object(
                agent_eval,
                "collect_graded_samples",
                return_value=([], agent_eval.SkippedCounts()),
            ),
            patch.object(agent_eval, "compute_label_baselines", return_value={}),
            patch.object(
                agent_eval,
                "write_report",
                return_value=(Path("/tmp/r.json"), Path("/tmp/r.md")),
            ),
            patch("shutil.copyfile"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            csv_path = Path(tmp) / "triage_notes.csv"
            csv_path.write_text("sample_id,category,outcome\n", encoding="utf-8")
            out_dir = Path(tmp) / "out"
            rc = agent_eval.main(
                [
                    "--csv",
                    str(csv_path),
                    "--out",
                    str(out_dir),
                    "--trace-dir",
                    str(Path(tmp) / "traces"),
                    "--judge",
                ]
            )
        # main() must surface the post-step's exit code verbatim.
        self.assertEqual(rc, 7)
        mock_post.assert_called_once()
        # ``--trace-dir`` was honoured and threaded through.
        called_kwargs = mock_post.call_args.kwargs
        self.assertEqual(called_kwargs["trace_dir"], Path(tmp) / "traces")

    def test_main_without_judge_does_not_call_post_step(self) -> None:
        """The default eval path (no --judge) must not invoke the
        post-step at all — locking in that the flag is opt-in keeps
        existing pytest + CI flows unchanged."""
        with (
            patch.object(agent_eval, "_run_judge_post_step") as mock_post,
            patch.object(agent_eval, "load_golden_set", return_value=[]),
            patch.object(
                agent_eval,
                "filter_golden_set_for_failure_memory_overlap",
                return_value=([], agent_eval.GoldenSetFilterSummary()),
            ),
            patch.object(agent_eval.rag, "hydrate_all"),
            patch.object(
                agent_eval,
                "collect_graded_samples",
                return_value=([], agent_eval.SkippedCounts()),
            ),
            patch.object(agent_eval, "compute_label_baselines", return_value={}),
            patch.object(
                agent_eval,
                "write_report",
                return_value=(Path("/tmp/r.json"), Path("/tmp/r.md")),
            ),
            patch("shutil.copyfile"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            csv_path = Path(tmp) / "triage_notes.csv"
            csv_path.write_text("sample_id,category,outcome\n", encoding="utf-8")
            out_dir = Path(tmp) / "out"
            rc = agent_eval.main(
                ["--csv", str(csv_path), "--out", str(out_dir)]
            )
        self.assertEqual(rc, 0)
        mock_post.assert_not_called()

    def test_main_writes_report_beside_explicit_traces_dir(self) -> None:
        """Resume mode should complete the original eval/runs/<stamp> dir,
        not create a fresh archive timestamp for the final report."""
        with (
            patch.object(agent_eval, "load_golden_set", return_value=[]),
            patch.object(
                agent_eval,
                "filter_golden_set_for_failure_memory_overlap",
                return_value=([], agent_eval.GoldenSetFilterSummary()),
            ),
            patch.object(agent_eval.rag, "hydrate_all"),
            patch.object(
                agent_eval,
                "collect_graded_samples",
                return_value=([], agent_eval.SkippedCounts()),
            ),
            patch.object(agent_eval, "compute_label_baselines", return_value={}),
            patch.object(
                agent_eval,
                "write_report",
                return_value=(Path("/tmp/r.json"), Path("/tmp/r.md")),
            ) as mock_write,
            patch("shutil.copyfile"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            csv_path = Path(tmp) / "triage_notes.csv"
            csv_path.write_text("sample_id,category,outcome\n", encoding="utf-8")
            out_dir = Path(tmp) / "eval"
            resumed_archive_dir = out_dir / "runs" / "2026-05-30T04-11-14Z"
            trace_dir = resumed_archive_dir / "traces"
            rc = agent_eval.main(
                [
                    "--csv",
                    str(csv_path),
                    "--out",
                    str(out_dir),
                    "--trace-dir",
                    str(trace_dir),
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(mock_write.call_args.args[1], resumed_archive_dir)


if __name__ == "__main__":
    unittest.main()
