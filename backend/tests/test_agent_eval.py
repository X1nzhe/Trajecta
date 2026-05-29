from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app import agent_eval


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


if __name__ == "__main__":
    unittest.main()
