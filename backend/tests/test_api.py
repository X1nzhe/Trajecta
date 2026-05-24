from __future__ import annotations

import io
import json
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
        self.previous_openai_api_key = os.environ.get("OPENAI_API_KEY")
        self.previous_agent_model = os.environ.get("TRAJECTA_AGENT_MODEL")
        self.previous_vlm_model = os.environ.get("TRAJECTA_VLM_MODEL")
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma_runtime")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("TRAJECTA_AGENT_MODEL", None)
        os.environ.pop("TRAJECTA_VLM_MODEL", None)
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
        if self.previous_openai_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.previous_openai_api_key
        if self.previous_agent_model is None:
            os.environ.pop("TRAJECTA_AGENT_MODEL", None)
        else:
            os.environ["TRAJECTA_AGENT_MODEL"] = self.previous_agent_model
        if self.previous_vlm_model is None:
            os.environ.pop("TRAJECTA_VLM_MODEL", None)
        else:
            os.environ["TRAJECTA_VLM_MODEL"] = self.previous_vlm_model
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

    def test_get_step_detail_accepts_image_detail_query(self) -> None:
        response = self.client.get("/api/runs/run_api/steps/0/detail", params={"image_detail": "low"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["image_detail"], "low")

    def test_get_step_detail_missing_step_returns_404(self) -> None:
        response = self.client.get("/api/runs/run_api/steps/99/detail")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "step not found")

    def test_get_step_detail_invalid_image_detail_returns_422(self) -> None:
        response = self.client.get("/api/runs/run_api/steps/0/detail", params={"image_detail": "medium"})

        self.assertEqual(response.status_code, 422)

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

    def test_analyze_returns_ndjson_events_before_done(self) -> None:
        from PIL import Image

        png = io.BytesIO()
        Image.new("RGB", (1, 1), color=(255, 255, 255)).save(png, format="PNG")
        storage.save_screenshots("run_api", {"screenshot_001.png": png.getvalue()})

        response = self.client.post("/api/runs/run_api/analyze")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", response.headers["content-type"])

        lines = [json.loads(line) for line in response.text.splitlines()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all(line["type"] == "event" for line in lines[:-1]))
        self.assertEqual(lines[-1]["type"], "done")
        self.assertEqual(lines[0]["event"]["seq"], 0)


if __name__ == "__main__":
    unittest.main()
