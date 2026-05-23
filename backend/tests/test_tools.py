from __future__ import annotations

import json
import os
import tempfile
import unittest

from backend.app import storage, tools
from backend.app.ids import make_eval_case_id
from backend.app.schemas import FailureMemoryCase
from backend.tests.test_storage import sample_eval_case, sample_run


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get("TRAJECTA_DATA_DIR")
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop("TRAJECTA_DATA_DIR", None)
        else:
            os.environ["TRAJECTA_DATA_DIR"] = self.previous_data_dir
        self.tmp.cleanup()

    def test_get_run_returns_run_without_fake_digest(self) -> None:
        storage.save_run(sample_run())

        result = tools.get_run("run_1")

        self.assertEqual(result["run_id"], "run_1")
        self.assertNotIn("digest", result)

    def test_propose_eval_case_generates_valid_draft(self) -> None:
        storage.save_run(sample_run())

        draft = tools.propose_eval_case(
            run_id="run_1",
            failure_step=0,
            failure_type="early_terminated",
            expected_behavior="The agent should finish the task.",
            actual_behavior="The agent stopped before finishing.",
            evidence=[{"claim": "Step 0 stopped.", "source": "trajectory", "run_id": "run_1", "step_index": 0}],
            regression_rule="Do not stop before task evidence is visible.",
            retrieved_context_ids=["fm_early_terminated_001"],
        )

        self.assertEqual(draft["case_id"], "ec_run_1_step_0")
        self.assertFalse(draft["human_validated"])
        self.assertEqual(draft["task"], "Find a result")

    def test_make_eval_case_id_handles_collision(self) -> None:
        class FakeStorage:
            @staticmethod
            def eval_case_exists(case_id: str) -> bool:
                return case_id == "ec_run_1_step_0"

        self.assertEqual(
            make_eval_case_id("run_1", 0, "early_terminated", storage_module=FakeStorage),
            "ec_run_1_step_0_early_terminated",
        )

    def test_fallback_search_failure_memory_returns_seeded_matches(self) -> None:
        cases_dir = storage.data_dir() / "failure_memory"
        cases_dir.mkdir(parents=True)
        case = FailureMemoryCase(
            case_id="fm_constraint",
            failure_type="missed_constraint",
            summary="The agent ignored a user constraint.",
            tags=["constraint"],
        )
        (cases_dir / "cases.jsonl").write_text(
            json.dumps(case.model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )

        results = tools.search_failure_memory("constraint", top_k=3)

        self.assertEqual(results[0]["case_id"], "fm_constraint")

    def test_find_similar_successful_run_filters_only_success_runs(self) -> None:
        storage.save_run(sample_run("success_run", status="success"))
        storage.save_run(sample_run("failed_run", status="failed"))

        results = tools.find_similar_successful_run("Find a result", top_k=3)

        self.assertEqual([result["run_id"] for result in results], ["success_run"])
        self.assertEqual(results[0]["status"], "success")

    def test_search_eval_cases_returns_local_matches(self) -> None:
        storage.save_eval_case(sample_eval_case("ec_run_1_step_0"))

        results = tools.search_eval_cases("early terminated", top_k=3)

        self.assertEqual(results[0]["case_id"], "ec_run_1_step_0")


if __name__ == "__main__":
    unittest.main()
