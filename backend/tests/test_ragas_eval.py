from __future__ import annotations

import os
import unittest
from unittest import mock

from backend.app import ragas_eval
from backend.app.schemas import AgentTrace, AgentTraceEvent


def _proposal_event() -> AgentTraceEvent:
    return AgentTraceEvent(
        seq=0,
        type="tool_call",
        name="propose_eval_case",
        args={
            "actual_behavior": "The agent stopped early.",
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
        sample = ragas_eval.RagasSample(
            run_id="run_1",
            question=ragas_eval.RAGAS_QUESTION,
            answer="The agent stopped early.\n\nStep 0 ended before the task was complete.",
            contexts=["fm_early_terminated_001: early stop [termination]"],
            ground_truth="early_terminated",
            ground_truth_source="self",
            proposed_failure_type="early_terminated",
            retrieved_context_ids=["fm_early_terminated_001"],
        )

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


if __name__ == "__main__":
    unittest.main()
