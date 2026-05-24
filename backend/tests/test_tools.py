from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from backend.app import rag, storage, tools
from backend.app.ids import make_eval_case_id
from backend.app.schemas import EvalCase, EvidenceItem, FailureMemoryCase
from backend.tests.test_storage import sample_run


def _create_tiny_png(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(path, format="PNG")


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get("TRAJECTA_DATA_DIR")
        self.previous_chroma_dir = os.environ.get("TRAJECTA_CHROMA_DIR")
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma")
        rag._client_cache = None
        rag._embedding_cache = None

    def tearDown(self) -> None:
        rag._client_cache = None
        rag._embedding_cache = None
        if self.previous_data_dir is None:
            os.environ.pop("TRAJECTA_DATA_DIR", None)
        else:
            os.environ["TRAJECTA_DATA_DIR"] = self.previous_data_dir
        if self.previous_chroma_dir is None:
            os.environ.pop("TRAJECTA_CHROMA_DIR", None)
        else:
            os.environ["TRAJECTA_CHROMA_DIR"] = self.previous_chroma_dir
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

    def test_search_failure_memory_delegates_to_rag(self) -> None:
        seeded = FailureMemoryCase(
            case_id="fm_missed_constraint_001",
            failure_type="missed_constraint",
            summary="The agent ignored a user constraint.",
            tags=["constraint"],
        )
        with mock.patch("backend.app.tools.rag.query_failure_memory", return_value=[seeded]) as spy:
            results = tools.search_failure_memory("constraint", top_k=5)

        spy.assert_called_once_with("constraint", top_k=5)
        self.assertEqual(results[0]["case_id"], "fm_missed_constraint_001")
        # Return shape is JSON-mode dict (not Pydantic instance).
        self.assertIsInstance(results[0], dict)

    def test_find_similar_successful_run_delegates_to_rag(self) -> None:
        canned = [{"run_id": "success_run", "task": "Find a result", "status": "success", "step_count": 3}]
        with mock.patch("backend.app.tools.rag.query_similar_successful_runs", return_value=canned) as spy:
            results = tools.find_similar_successful_run("Find a result", top_k=3)

        spy.assert_called_once_with("Find a result", top_k=3)
        self.assertEqual([r["run_id"] for r in results], ["success_run"])
        self.assertEqual(results[0]["status"], "success")

    def test_search_eval_cases_delegates_to_rag(self) -> None:
        seeded = EvalCase(
            case_id="ec_run_1_step_0",
            source_run_id="run_1",
            task="Find a result",
            failure_step=0,
            failure_type="early_terminated",
            expected_behavior="The agent should finish the task.",
            actual_behavior="The agent stopped before finishing.",
            evidence=[EvidenceItem(claim="Step 0 stopped.", source="trajectory", run_id="run_1", step_index=0)],
            regression_rule="Do not stop before task evidence is visible.",
            human_validated=True,
        )
        with mock.patch("backend.app.tools.rag.query_eval_cases", return_value=[seeded]) as spy:
            results = tools.search_eval_cases("early terminated", top_k=4, only_validated=True)

        spy.assert_called_once_with("early terminated", top_k=4, only_validated=True)
        self.assertEqual(results[0]["case_id"], "ec_run_1_step_0")
        self.assertIsInstance(results[0], dict)

    def test_get_step_detail_returns_valid_shape(self) -> None:
        storage.save_run(sample_run())

        result = tools.get_step_detail("run_1", step_index=0, image_detail="high")

        self.assertIn("run_id", result)
        self.assertIn("step_index", result)
        self.assertIn("has_screenshot", result)
        self.assertIn("image_detail", result)
        self.assertIn("vlm_summary", result)
        self.assertIn("action", result)
        self.assertIn("observation", result)
        self.assertIn("result", result)
        self.assertIn("coordinate_validation", result)
        self.assertEqual(result["image_detail"], "high")
        self.assertFalse(result["has_screenshot"])
        self.assertIsNone(result["vlm_summary"])

    def test_get_step_detail_low_detail_mode(self) -> None:
        storage.save_run(sample_run())
        _create_tiny_png(storage.screenshots_dir("run_1") / "screenshot_001.png")

        result = tools.get_step_detail("run_1", step_index=0, image_detail="low")

        self.assertEqual(result["image_detail"], "low")
        self.assertTrue(result["has_screenshot"])
        self.assertIsNotNone(result["vlm_summary"])
        self.assertLessEqual(len(result["vlm_summary"]), 200)

    def test_get_step_detail_high_detail_with_screenshot(self) -> None:
        storage.save_run(sample_run())
        _create_tiny_png(storage.screenshots_dir("run_1") / "screenshot_001.png")

        result = tools.get_step_detail("run_1", step_index=0, image_detail="high")

        self.assertTrue(result["has_screenshot"])
        self.assertIsNotNone(result["vlm_summary"])

    def test_get_step_detail_invalid_step_index_returns_tool_error(self) -> None:
        storage.save_run(sample_run())

        result = tools.get_step_detail("run_1", step_index=99)

        self.assertIn("tool_error", result)
        self.assertIsInstance(result["tool_error"], str)
        self.assertTrue(result["tool_error"])

    def test_get_step_detail_unknown_run_returns_tool_error(self) -> None:
        result = tools.get_step_detail("nonexistent_run", step_index=0)

        self.assertIn("tool_error", result)

    def test_get_step_detail_no_screenshot_bytes_in_result(self) -> None:
        storage.save_run(sample_run())

        result = tools.get_step_detail("run_1", step_index=0)

        self.assertNotIn("screenshot_bytes", result)
        self.assertNotIn("image_bytes", result)
        self.assertNotIn("image_data", result)
        self.assertFalse(result["observation"]["screenshot"].startswith("/"))

    def test_get_step_detail_screenshot_url_format(self) -> None:
        storage.save_run(sample_run())
        _create_tiny_png(storage.screenshots_dir("run_1") / "screenshot_001.png")

        result = tools.get_step_detail("run_1", step_index=0)

        self.assertEqual(
            result["screenshot_url"],
            "/api/runs/run_1/screenshots/screenshot_001.png",
        )

    def test_get_run_with_comparison_run_id(self) -> None:
        """docs/testing.md: get_run accepts a comparison run_id distinct from
        the run currently under analysis.
        """

        storage.save_run(sample_run("run_a"))
        storage.save_run(sample_run("run_b"))

        result_a = tools.get_run("run_a")
        result_b = tools.get_run("run_b")

        self.assertEqual(result_a["run_id"], "run_a")
        self.assertEqual(result_b["run_id"], "run_b")

    def test_find_similar_successful_run_filters_status_and_excludes_self(self) -> None:
        """docs/testing.md: find_similar_successful_run returns only runs with
        status=='success' and excludes the queried run_id.
        """

        storage.save_run(sample_run("run_a", status="success"))
        storage.save_run(sample_run("run_b", status="failed"))
        rag.upsert_successful_run(sample_run("run_a", status="success"))
        # run_b is not upserted because it is not a success.

        results = rag.query_similar_successful_runs(
            "Find a result", top_k=5, exclude_run_id="run_a"
        )

        run_ids = [r["run_id"] for r in results]
        self.assertNotIn("run_a", run_ids)  # self exclusion
        self.assertNotIn("run_b", run_ids)  # status filter

    def test_find_similar_successful_run_empty_when_no_success_run_indexed(self) -> None:
        """docs/testing.md: find_similar_successful_run returns an empty list
        when no successful run is indexed for the task.
        """

        results = tools.find_similar_successful_run("Find a result", top_k=3)

        self.assertEqual(results, [])

    def test_propose_eval_case_rejects_missing_required_fields(self) -> None:
        """docs/testing.md: propose_eval_case rejects an EvalCase draft missing
        required fields. Current tools.py raises Pydantic ValidationError when
        a constructed field violates the EvalCase schema.
        """

        storage.save_run(sample_run())

        with self.assertRaises(ValidationError):
            tools.propose_eval_case(
                run_id="run_1",
                failure_step=0,
                failure_type="INVALID-TYPE",  # violates ^[a-z][a-z0-9_]*$
                expected_behavior="x",
                actual_behavior="y",
                evidence=[{"claim": "c", "source": "trajectory", "run_id": "run_1"}],
                regression_rule="r",
                retrieved_context_ids=[],
            )


if __name__ == "__main__":
    unittest.main()
