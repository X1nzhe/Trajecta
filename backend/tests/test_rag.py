from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import pytest

from backend.app import rag, storage, tools
from backend.app.schemas import (
    EvalCase,
    EvidenceItem,
    FailureMemoryCase,
)
from backend.tests.test_storage import sample_run


def _sample_failure_memory(
    case_id: str = "fm_missed_constraint_001",
    failure_type: str = "missed_constraint",
    summary: str = "The agent ignored the price filter.",
    tags: list[str] | None = None,
    fix_hint: str | None = "re-check filter UI before clicking results",
) -> FailureMemoryCase:
    return FailureMemoryCase(
        case_id=case_id,
        failure_type=failure_type,
        summary=summary,
        fix_hint=fix_hint,
        tags=tags or ["constraint", "filter"],
        source_run_id=None,
    )


def _sample_eval_case(case_id: str = "ec_run_42_step_3", human_validated: bool = True) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        source_run_id="run_42",
        task="Filter results under twenty dollars",
        failure_step=3,
        failure_type="missed_constraint",
        expected_behavior="apply price filter then click first result",
        actual_behavior="clicked first result with no filter applied",
        evidence=[
            EvidenceItem(claim="price filter input untouched", source="trajectory_digest", run_id="run_42", step_index=2),
            EvidenceItem(claim="result page lists items above twenty", source="step_detail_high", run_id="run_42", step_index=3),
        ],
        regression_rule="Always apply price constraint before navigating to results.",
        retrieved_context_ids=["fm_missed_constraint_001"],
        human_validated=human_validated,
    )


class RagPerTestEnv(unittest.TestCase):
    """Per-test isolation for TRAJECTA_DATA_DIR and TRAJECTA_CHROMA_DIR.

    Each test sets both env vars to a fresh tmpdir and resets the
    module-level chroma client cache so factory probing actually runs.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {
            "TRAJECTA_DATA_DIR": os.environ.get("TRAJECTA_DATA_DIR"),
            "TRAJECTA_CHROMA_DIR": os.environ.get("TRAJECTA_CHROMA_DIR"),
        }
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        # Use a sibling, distinct directory so default-resolution logic
        # (data_dir() / "chroma") is exercised separately in another test.
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma_runtime")
        rag._client_cache = None

    def tearDown(self) -> None:
        rag._client_cache = None
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()


class FailureMemoryTests(RagPerTestEnv):
    def test_failure_memory_upsert_then_query_returns_seeded_case(self) -> None:
        case = _sample_failure_memory()
        rag.upsert_failure_memory(case)

        results = rag.query_failure_memory("missed constraint price filter", top_k=3)

        self.assertGreaterEqual(len(results), 1)
        ids = [r.case_id for r in results]
        self.assertIn(case.case_id, ids)
        roundtripped = next(r for r in results if r.case_id == case.case_id)
        self.assertEqual(roundtripped.summary, case.summary)
        self.assertEqual(roundtripped.failure_type, "missed_constraint")

    def test_failure_memory_upsert_is_idempotent_on_case_id(self) -> None:
        case = _sample_failure_memory(summary="original summary")
        rag.upsert_failure_memory(case)
        rag.upsert_failure_memory(case)
        rag.upsert_failure_memory(case.model_copy(update={"summary": "updated summary"}))

        col = rag.failure_memory_collection()
        self.assertEqual(col.count(), 1)
        latest = rag.query_failure_memory("anything", top_k=3)[0]
        self.assertEqual(latest.summary, "updated summary")

    def test_failure_memory_tags_roundtrip(self) -> None:
        case = _sample_failure_memory(tags=["constraint", "price", "filter"])
        rag.upsert_failure_memory(case)

        result = rag.query_failure_memory("anything", top_k=3)[0]

        self.assertEqual(sorted(result.tags), ["constraint", "filter", "price"])


class EvalCaseTests(RagPerTestEnv):
    def test_eval_cases_upsert_refuses_drafts(self) -> None:
        draft = _sample_eval_case(human_validated=False)

        with self.assertRaises(ValueError):
            rag.upsert_eval_case(draft)

        self.assertEqual(rag.eval_cases_collection().count(), 0)

    def test_eval_cases_query_reconstructs_full_evidence(self) -> None:
        case = _sample_eval_case()
        rag.upsert_eval_case(case)

        results = rag.query_eval_cases(case.task, top_k=3)

        self.assertEqual(len(results), 1)
        reconstructed = results[0]
        self.assertEqual(len(reconstructed.evidence), 2)
        self.assertEqual(reconstructed.retrieved_context_ids, ["fm_missed_constraint_001"])
        # Full Pydantic round-trip.
        EvalCase.model_validate(reconstructed.model_dump(mode="json"))


class SuccessfulRunsTests(RagPerTestEnv):
    def test_successful_runs_upsert_refuses_non_success_status(self) -> None:
        run = sample_run("flaky_run", status="failed")

        with self.assertRaises(ValueError):
            rag.upsert_successful_run(run)

        self.assertEqual(rag.successful_runs_collection().count(), 0)

    def test_successful_runs_exclude_run_id_filters_self(self) -> None:
        rag.upsert_successful_run(sample_run("run_a", status="success"))
        rag.upsert_successful_run(sample_run("run_b", status="success"))

        results = rag.query_similar_successful_runs("Find a result", top_k=3, exclude_run_id="run_a")

        ids = [r["run_id"] for r in results]
        self.assertNotIn("run_a", ids)
        self.assertIn("run_b", ids)

    def test_delete_successful_run_removes_row(self) -> None:
        rag.upsert_successful_run(sample_run("run_a", status="success"))
        rag.upsert_successful_run(sample_run("run_b", status="success"))

        rag.delete_successful_run("run_a")

        self.assertEqual(rag.successful_runs_collection().count(), 1)
        ids = [r["run_id"] for r in rag.query_similar_successful_runs("Find a result", top_k=5)]
        self.assertNotIn("run_a", ids)


class ToolReturnShapeTests(RagPerTestEnv):
    def test_query_returns_no_score_or_distance_keys(self) -> None:
        rag.upsert_failure_memory(_sample_failure_memory())

        results = tools.search_failure_memory("constraint", top_k=3)

        self.assertGreaterEqual(len(results), 1)
        allowed = {"case_id", "failure_type", "summary", "fix_hint", "tags", "source_run_id"}
        for item in results:
            self.assertEqual(set(item.keys()), allowed)
            self.assertNotIn("score", item)
            self.assertNotIn("distance", item)
            self.assertNotIn("similarity", item)


class HydrationTests(RagPerTestEnv):
    def test_hydrate_all_is_idempotent(self) -> None:
        cases_dir = storage.data_dir() / "failure_memory"
        cases_dir.mkdir(parents=True)
        (cases_dir / "cases.jsonl").write_text(
            json.dumps(_sample_failure_memory().model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )
        storage.save_run(sample_run("ok_run", status="success"))
        storage.save_eval_case(_sample_eval_case())

        rag.hydrate_all()
        before_counts = (
            rag.failure_memory_collection().count(),
            rag.eval_cases_collection().count(),
            rag.successful_runs_collection().count(),
        )
        rag.hydrate_all()
        after_counts = (
            rag.failure_memory_collection().count(),
            rag.eval_cases_collection().count(),
            rag.successful_runs_collection().count(),
        )
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_counts, (1, 1, 1))

    def test_hydrate_all_loads_failure_memory_from_jsonl(self) -> None:
        cases_dir = storage.data_dir() / "failure_memory"
        cases_dir.mkdir(parents=True)
        case = _sample_failure_memory(case_id="fm_missed_constraint_002", tags=["constraint"])
        (cases_dir / "cases.jsonl").write_text(
            json.dumps(case.model_dump(mode="json")) + "\n",
            encoding="utf-8",
        )

        rag.hydrate_all()

        results = rag.query_failure_memory("constraint", top_k=3)
        self.assertIn("fm_missed_constraint_002", [r.case_id for r in results])


class ChromaDirOverrideTests(unittest.TestCase):
    def test_chroma_dir_respects_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as data_tmp, tempfile.TemporaryDirectory() as chroma_tmp:
            saved_data = os.environ.get("TRAJECTA_DATA_DIR")
            saved_chroma = os.environ.get("TRAJECTA_CHROMA_DIR")
            os.environ["TRAJECTA_DATA_DIR"] = data_tmp
            os.environ["TRAJECTA_CHROMA_DIR"] = chroma_tmp
            rag._client_cache = None
            try:
                col = rag.failure_memory_collection()
                col.upsert(ids=["probe"], documents=["hello"], metadatas=[{"k": "v"}])
                # chromadb writes a sqlite file plus collection segment dirs.
                chroma_listing = os.listdir(chroma_tmp)
                self.assertTrue(
                    any(name.startswith("chroma") or name.endswith(".sqlite3") for name in chroma_listing),
                    f"expected chromadb artifacts in override dir, got: {chroma_listing}",
                )
                # Default location under TRAJECTA_DATA_DIR/chroma must not have been used.
                data_chroma = os.path.join(data_tmp, "chroma")
                self.assertFalse(
                    os.path.exists(data_chroma) and os.listdir(data_chroma),
                    "default chroma dir should be empty when override is set",
                )
            finally:
                rag._client_cache = None
                if saved_data is None:
                    os.environ.pop("TRAJECTA_DATA_DIR", None)
                else:
                    os.environ["TRAJECTA_DATA_DIR"] = saved_data
                if saved_chroma is None:
                    os.environ.pop("TRAJECTA_CHROMA_DIR", None)
                else:
                    os.environ["TRAJECTA_CHROMA_DIR"] = saved_chroma


class EmbeddingFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "TRAJECTA_USE_FAKE_EMBEDDING": os.environ.pop("TRAJECTA_USE_FAKE_EMBEDDING", None),
            "TRAJECTA_EMBEDDING_MODEL": os.environ.pop("TRAJECTA_EMBEDDING_MODEL", None),
            "OPENAI_API_KEY": os.environ.pop("OPENAI_API_KEY", None),
        }

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_embedding_factory_falls_back_to_default_when_openai_missing(self) -> None:
        os.environ["TRAJECTA_EMBEDDING_MODEL"] = "text-embedding-3-small"
        # No OPENAI_API_KEY → default embedder.
        ef = rag.get_embedding_function()
        self.assertNotIsInstance(ef, rag.FakeEmbeddingFunction)
        self.assertNotIn("OpenAI", type(ef).__name__)

        # With API key set but openai not importable → still default.
        os.environ["OPENAI_API_KEY"] = "test-key"
        with mock.patch.dict(sys.modules, {"openai": None}):
            ef2 = rag.get_embedding_function()
        self.assertNotIn("OpenAI", type(ef2).__name__)

    def test_fake_embedding_function_is_deterministic_and_normalized(self) -> None:
        os.environ["TRAJECTA_USE_FAKE_EMBEDDING"] = "1"
        ef = rag.get_embedding_function()
        self.assertIsInstance(ef, rag.FakeEmbeddingFunction)
        # Coerce to plain lists — chromadb wraps EmbeddingFunction outputs
        # in numpy arrays, whose `==` would yield element-wise comparisons.
        a = list(ef(["hello world"])[0])
        b = list(ef(["hello world"])[0])
        c = list(ef(["different text"])[0])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        norm = sum(x * x for x in a) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)


class SeedFailureMemoryFileTests(unittest.TestCase):
    """Validate the on-disk seed file, independent of TRAJECTA_DATA_DIR overrides."""

    def test_failure_memory_seed_contains_five_cases_including_missed_constraint(self) -> None:
        """docs/testing.md: failure memory seed contains at least 5 cases
        including missed_constraint.
        """

        from backend.app.storage import REPO_ROOT

        seed = REPO_ROOT / "data" / "failure_memory" / "cases.jsonl"
        self.assertTrue(seed.exists(), f"missing seed file: {seed}")

        cases: list[FailureMemoryCase] = []
        for line in seed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            cases.append(FailureMemoryCase.model_validate_json(line))

        self.assertGreaterEqual(len(cases), 5)
        self.assertTrue(any(c.failure_type == "missed_constraint" for c in cases))


class RankingAndTopKTests(RagPerTestEnv):
    def test_find_similar_successful_run_same_task_outranks_cross_task(self) -> None:
        """docs/testing.md: find_similar_successful_run returns higher
        similarity for same-task runs than for cross-task runs.
        """

        same_task_run = sample_run("run_same", status="success").model_copy(
            update={"task": "Filter results under twenty dollars"}
        )
        cross_task_run = sample_run("run_cross", status="success").model_copy(
            update={"task": "Send a message to a friend"}
        )
        rag.upsert_successful_run(same_task_run)
        rag.upsert_successful_run(cross_task_run)

        results = rag.query_similar_successful_runs(
            "Filter results under twenty dollars", top_k=2
        )

        run_ids = [r["run_id"] for r in results]
        self.assertIn("run_same", run_ids)
        # Same-task run must appear before cross-task run when both are present.
        if "run_cross" in run_ids:
            self.assertLess(run_ids.index("run_same"), run_ids.index("run_cross"))

    def test_top_k_length_respected(self) -> None:
        """docs/testing.md: top_k length is respected."""

        for i in range(5):
            rag.upsert_failure_memory(
                _sample_failure_memory(
                    case_id=f"fm_missed_constraint_{i + 1:03d}",
                    summary=f"case {i}",
                )
            )

        results = rag.query_failure_memory("constraint", top_k=2)
        self.assertEqual(len(results), 2)


@pytest.mark.skipif(
    os.environ.get("TRAJECTA_RAG_INTEGRATION") != "1",
    reason="opt-in: real sentence-transformers embedder downloads a ~80MB model",
)
def test_default_embedder_real_model_roundtrip(tmp_path, monkeypatch):
    """Opt-in: exercise the real DefaultEmbeddingFunction once before shipping."""

    monkeypatch.delenv("TRAJECTA_USE_FAKE_EMBEDDING", raising=False)
    monkeypatch.setenv("TRAJECTA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRAJECTA_CHROMA_DIR", str(tmp_path / "chroma"))
    rag._client_cache = None
    try:
        ef = rag.get_embedding_function()
        assert not isinstance(ef, rag.FakeEmbeddingFunction)
        rag.upsert_failure_memory(_sample_failure_memory())
        results = rag.query_failure_memory("constraint price filter", top_k=3)
        assert any(r.case_id == "fm_missed_constraint_001" for r in results)
    finally:
        rag._client_cache = None


if __name__ == "__main__":
    unittest.main()
