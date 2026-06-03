"""Tests for the EvalCase draft contract.

Mirrors docs/testing.md "tests/test_eval_case.py":
- agent eval_case_draft validates against the EvalCase contract
- eval_case_draft evidence validates as structured EvidenceItem rows
- exported eval case validates against the EvalCase contract
"""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend.app import eval_agent_graph, rag, storage
from backend.app.main import app
from backend.app.schemas import EvalCase, EvidenceItem, FailureMemoryCase
from backend.tests.test_storage import sample_run


class EvalCaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {
            "TRAJECTA_DATA_DIR": os.environ.get("TRAJECTA_DATA_DIR"),
            "TRAJECTA_CHROMA_DIR": os.environ.get("TRAJECTA_CHROMA_DIR"),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL"),
            "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
            "GEMINI_BASE_URL": os.environ.get("GEMINI_BASE_URL"),
            "TRAJECTA_AGENT_MODEL": os.environ.get("TRAJECTA_AGENT_MODEL"),
            "TRAJECTA_VLM_MODEL": os.environ.get("TRAJECTA_VLM_MODEL"),
        }
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_BASE_URL", None)
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GEMINI_BASE_URL", None)
        os.environ.pop("TRAJECTA_AGENT_MODEL", None)
        os.environ.pop("TRAJECTA_VLM_MODEL", None)
        rag._client_cache = None
        rag._embedding_cache = None

        storage.save_trajectory(sample_run("run_1", status="failed"))
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="The agent ignored a user constraint.",
                fix_hint="Re-check constraints before completion.",
                tags=["constraint"],
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        rag._client_cache = None
        rag._embedding_cache = None
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_agent_eval_case_draft_validates_against_contract(self) -> None:
        result = eval_agent_graph.analyze_trajectory("run_1")

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        case = EvalCase.model_validate(result.eval_case_draft)
        self.assertFalse(case.human_validated)
        self.assertEqual(case.source_trajectory_id, "run_1")

    def test_eval_case_draft_evidence_rows_validate(self) -> None:
        result = eval_agent_graph.analyze_trajectory("run_1")

        draft = result.eval_case_draft
        self.assertIsNotNone(draft)
        evidence_rows = draft["evidence"]
        self.assertGreater(len(evidence_rows), 0)
        for row in evidence_rows:
            item = EvidenceItem.model_validate(row)
            self.assertTrue(item.claim)

    def test_exported_eval_case_validates_against_contract(self) -> None:
        result = eval_agent_graph.analyze_trajectory("run_1")
        draft = dict(result.eval_case_draft)
        draft["human_validated"] = True

        response = self.client.post("/api/eval-cases", json=draft)

        self.assertEqual(response.status_code, 200, response.text)
        persisted = EvalCase.model_validate(response.json())
        self.assertTrue(persisted.human_validated)
        self.assertEqual(persisted.case_id, draft["case_id"])


if __name__ == "__main__":
    unittest.main()
