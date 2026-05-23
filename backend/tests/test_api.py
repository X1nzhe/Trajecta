from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.main import app
from backend.tests.test_storage import sample_eval_case, sample_run


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get("TRAJECTA_DATA_DIR")
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        storage.save_run(sample_run("run_api"))
        storage.save_screenshots("run_api", {"screenshot_001.png": b"not-a-real-png"})
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop("TRAJECTA_DATA_DIR", None)
        else:
            os.environ["TRAJECTA_DATA_DIR"] = self.previous_data_dir
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


if __name__ == "__main__":
    unittest.main()
