from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import dataset_importer, rag, storage, tools
from backend.app.main import app
from backend.tests.test_dataset_importer import raw_row
from backend.tests.test_storage import sample_eval_case, sample_run


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get("TRAJECTA_DATA_DIR")
        self.previous_chroma_dir = os.environ.get("TRAJECTA_CHROMA_DIR")
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma_runtime")
        rag._client_cache = None
        storage.save_run(sample_run("run_api"))
        storage.save_screenshots("run_api", {"screenshot_001.png": b"not-a-real-png"})
        self.client = TestClient(app)

    def tearDown(self) -> None:
        rag._client_cache = None
        if self.previous_data_dir is None:
            os.environ.pop("TRAJECTA_DATA_DIR", None)
        else:
            os.environ["TRAJECTA_DATA_DIR"] = self.previous_data_dir
        if self.previous_chroma_dir is None:
            os.environ.pop("TRAJECTA_CHROMA_DIR", None)
        else:
            os.environ["TRAJECTA_CHROMA_DIR"] = self.previous_chroma_dir
        self.tmp.cleanup()

    def test_get_runs(self) -> None:
        response = self.client.get("/api/runs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["run_id"], "run_api")

    def test_get_run(self) -> None:
        response = self.client.get("/api/runs/run_api")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"], "Find a result")

    def test_get_step(self) -> None:
        response = self.client.get("/api/runs/run_api/steps/0")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["index"], 0)

    def test_screenshot_traversal_rejected(self) -> None:
        response = self.client.get("/api/runs/run_api/screenshots/%2E%2E/trajectory.json")

        self.assertEqual(response.status_code, 404)

    def test_post_eval_case_rejects_unvalidated(self) -> None:
        case = sample_eval_case("ec_run_api_step_0").model_copy(update={"human_validated": False})

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(response.status_code, 422)

    def test_duplicate_eval_case_returns_409(self) -> None:
        case = sample_eval_case("ec_run_api_step_0")

        first = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))
        second = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    def test_post_eval_case_upserts_into_rag(self) -> None:
        case = sample_eval_case("ec_run_api_step_0")

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))
        self.assertEqual(response.status_code, 200)

        # Search goes tools.search_eval_cases → rag.query_eval_cases against
        # the live ChromaDB collection populated by the POST handler.
        search = self.client.get("/api/eval-cases/search", params={"q": "early terminated", "top_k": 5})
        self.assertEqual(search.status_code, 200)
        ids = [item["case_id"] for item in search.json()]
        self.assertIn("ec_run_api_step_0", ids)

    def test_import_handler_upserts_success_runs_into_rag(self) -> None:
        # Force the importer to yield one success row without needing a real
        # HuggingFace dataset on disk. Mirrors the pattern used in
        # test_dataset_importer.test_image_paths_null_mapping_does_not_fail.
        original_loader = dataset_importer._load_dataset_from_disk
        source_dir = Path(self.tmp.name) / "fake_hf"
        source_dir.mkdir(parents=True)
        success_row = raw_row(sample_id="imported_success", status="success")
        dataset_importer._load_dataset_from_disk = lambda path: [success_row]
        try:
            response = self.client.post(
                "/api/import/molmoweb-sample",
                json={"source_dir": str(source_dir)},
            )
        finally:
            dataset_importer._load_dataset_from_disk = original_loader

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["imported_count"], 1)
        self.assertEqual(response.json()["runs"][0]["status"], "success")

        results = tools.find_similar_successful_run("Find the checkout button.", top_k=3)
        self.assertIn("imported_success", [r["run_id"] for r in results])


if __name__ == "__main__":
    unittest.main()
