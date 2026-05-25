from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import dataset_importer, eval_agent_graph, rag, storage, tools
from backend.app.eval_agent_graph import AIMessage
from backend.app.main import app
from backend.app.schemas import EvalCase, EvidenceItem, FailureMemoryCase
from backend.tests.test_dataset_importer import raw_row
from backend.tests.test_storage import sample_eval_case, sample_run


def drain_ndjson(response) -> tuple[list[dict], dict]:
    """Drain a streaming NDJSON response. Returns (events, terminal_line).

    Short-circuits with ([], {}) when response.status_code != 200 — HTTP
    4xx are NOT streamed (see docs/testing.md). Splits response.text by
    newline, json.loads each non-empty line, and partitions into events
    (type=="event") vs terminal (type in {"done","error"}).
    """

    if response.status_code != 200:
        return [], {}
    events: list[dict] = []
    terminal: dict = {}
    for line in response.text.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("type") == "event":
            events.append(record["event"])
        elif record.get("type") in {"done", "error"}:
            terminal = record
    return events, terminal


def _write_real_png(run_id: str, filename: str = "screenshot_001.png") -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(buf, format="PNG")
    storage.save_screenshots(run_id, {filename: buf.getvalue()})


class _ScriptedLLM:
    """Mirror of test_eval_agent.ScriptedLLM, routed through API monkey-patch."""

    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.invocations = 0

    def invoke(self, messages: list) -> object:
        if self.invocations >= len(self.messages):
            return AIMessage(content="no more scripted messages")
        message = self.messages[self.invocations]
        self.invocations += 1
        return message


def _tool_message(name: str, args: dict) -> object:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def _proposal_args(
    *,
    run_id: str = "run_api",
    failure_type: str = "missed_constraint",
    retrieved_context_ids: list[str] | None = None,
) -> dict:
    retrieved_context_ids = (
        retrieved_context_ids if retrieved_context_ids is not None else ["fm_missed_constraint_001"]
    )
    evidence: list[dict] = [
        {
            "claim": "Step 0 was inspected.",
            "source": "trajectory",
            "run_id": run_id,
            "step_index": 0,
        }
    ]
    if retrieved_context_ids:
        evidence.append(
            {
                "claim": "Retrieved memory covers missed constraints.",
                "source": "failure_memory",
                "context_id": retrieved_context_ids[0],
            }
        )
    return {
        "run_id": run_id,
        "failure_step": 0,
        "failure_type": failure_type,
        "expected_behavior": "The agent should satisfy the task constraint.",
        "actual_behavior": "The trajectory does not show the constraint being satisfied.",
        "evidence": evidence,
        "regression_rule": "Check the constraint before completing the task.",
        "retrieved_context_ids": retrieved_context_ids,
    }


def _patched_default_llm(messages: list[object]):
    """Replacement for eval_agent_graph._default_llm_client tied to a script."""

    def factory(state):  # noqa: ARG001
        return _ScriptedLLM(list(messages))

    return factory


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

    def test_screenshot_path_traversal_rejected(self) -> None:
        response = self.client.get("/api/runs/run_api/screenshots/%2E%2E/trajectory.json")

        self.assertEqual(response.status_code, 404)

    def test_screenshot_missing_returns_404(self) -> None:
        response = self.client.get("/api/runs/run_api/screenshots/nonexistent.png")

        self.assertEqual(response.status_code, 404)

    def test_screenshot_endpoint_returns_fixture_image(self) -> None:
        _write_real_png("run_api")

        response = self.client.get("/api/runs/run_api/screenshots/screenshot_001.png")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_post_eval_case_rejects_unvalidated(self) -> None:
        case = sample_eval_case("ec_run_api_step_0", source_run_id="run_api").model_copy(update={"human_validated": False})

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(response.status_code, 422)

    def test_duplicate_eval_case_returns_409(self) -> None:
        case = sample_eval_case("ec_run_api_step_0", source_run_id="run_api")

        first = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))
        second = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    def test_post_eval_case_upserts_into_rag(self) -> None:
        case = sample_eval_case("ec_run_api_step_0", source_run_id="run_api")

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))
        self.assertEqual(response.status_code, 200)

        # Search goes tools.search_eval_cases → rag.query_eval_cases against
        # the live ChromaDB collection populated by the POST handler.
        search = self.client.get("/api/eval-cases/search", params={"q": "early terminated", "top_k": 5})
        self.assertEqual(search.status_code, 200)
        ids = [item["case_id"] for item in search.json()]
        self.assertIn("ec_run_api_step_0", ids)

    def test_post_failure_eval_case_flips_run_status_to_failed(self) -> None:
        """Validating a failure-shape EvalCase must flip the source run's
        status to 'failed' so the UI badge reflects the verdict.
        """

        from backend.tests.test_storage import sample_eval_case as case_factory

        case = case_factory("ec_run_api_step_0", source_run_id="run_api")
        self.assertEqual(storage.load_run("run_api").status, "unknown")

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.load_run("run_api").status, "failed")

    def test_post_success_eval_case_flips_status_and_seeds_rag(self) -> None:
        """Validating a success-shape EvalCase must flip the run to
        'success' AND upsert it into the successful_runs collection so
        find_similar_successful_run starts returning matches.
        """

        from backend.tests.test_storage import sample_success_eval_case

        case = sample_success_eval_case("ec_run_api_success", source_run_id="run_api")

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.load_run("run_api").status, "success")
        results = tools.find_similar_successful_run("Find a result", top_k=3)
        self.assertIn("run_api", [r["run_id"] for r in results])

    def test_post_eval_case_unknown_source_run_returns_404(self) -> None:
        from backend.tests.test_storage import sample_eval_case as case_factory

        case = case_factory("ec_ghost_step_0", source_run_id="ghost_run")

        response = self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        self.assertEqual(response.status_code, 404)

    def test_failure_validation_evicts_prior_success_rag_entry(self) -> None:
        """If a run was previously validated as success (and indexed into
        successful_runs), a subsequent failure validation must remove the
        stale row so find_similar_successful_run no longer returns it.
        """

        from backend.tests.test_storage import (
            sample_eval_case as failure_factory,
            sample_success_eval_case,
        )

        success_case = sample_success_eval_case("ec_run_api_success", source_run_id="run_api")
        first = self.client.post("/api/eval-cases", json=success_case.model_dump(mode="json"))
        self.assertEqual(first.status_code, 200)
        self.assertIn("run_api", [r["run_id"] for r in tools.find_similar_successful_run("Find a result", top_k=3)])

        failure_case = failure_factory("ec_run_api_step_0", source_run_id="run_api")
        second = self.client.post("/api/eval-cases", json=failure_case.model_dump(mode="json"))
        self.assertEqual(second.status_code, 200)

        self.assertEqual(storage.load_run("run_api").status, "failed")
        self.assertEqual(tools.find_similar_successful_run("Find a result", top_k=3), [])

    def test_import_handler_starts_cold(self) -> None:
        """v1 cold-start contract (docs/dataset_import.md): imported runs
        land at status='unknown' regardless of any raw `status` field, and
        the importer does not seed any RAG collection. RAG only grows via
        human-validated eval cases.
        """

        original_loader = dataset_importer._load_dataset_from_disk
        source_dir = Path(self.tmp.name) / "fake_hf"
        source_dir.mkdir(parents=True)
        # Even passing a raw success status: the importer no longer applies
        # the run_status_overlay, and the API handler no longer upserts
        # successful_runs at import time. The raw status flows through the
        # importer (normalize_trajectory honors it), but the handler does
        # not seed RAG from it.
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

        # Even though the raw row had status="success", the importer
        # overrides it to "unknown" per the cold-start contract.
        self.assertEqual(response.json()["runs"][0]["status"], "unknown")
        self.assertEqual(storage.load_run("imported_success").status, "unknown")

        # No RAG seeding from import — the successful_runs collection is
        # empty until a human validates a success EvalCase.
        results = tools.find_similar_successful_run("Find the checkout button.", top_k=3)
        self.assertEqual(results, [])

    def test_analyze_returns_ndjson_events_before_done(self) -> None:
        from PIL import Image

        png = io.BytesIO()
        Image.new("RGB", (1, 1), color=(255, 255, 255)).save(png, format="PNG")
        storage.save_screenshots("run_api", {"screenshot_001.png": png.getvalue()})

        response = self.client.post("/api/runs/run_api/analyze")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/x-ndjson", response.headers["content-type"])

        events, terminal = drain_ndjson(response)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(terminal["type"], "done")
        self.assertEqual(events[0]["seq"], 0)

    def test_list_runs_returns_at_least_5(self) -> None:
        for i in range(5):
            storage.save_run(sample_run(f"run_seed_{i}"))

        response = self.client.get("/api/runs")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()), 5)

    def test_analyze_done_line_carries_eval_case_draft_and_trace_meta(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )

        response = self.client.post("/api/runs/run_api/analyze")

        events, terminal = drain_ndjson(response)
        self.assertEqual(terminal["type"], "done")
        self.assertIsNotNone(terminal["eval_case_draft"])
        agent_trace = terminal["agent_trace"]
        self.assertIn("tool_call_count", agent_trace)
        self.assertIn("turn_count", agent_trace)
        self.assertIn("terminated_by", agent_trace)
        self.assertGreater(len(events), 0)

    def test_analyze_event_seqs_strictly_increasing_from_zero(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )

        response = self.client.post("/api/runs/run_api/analyze")
        events, _ = drain_ndjson(response)

        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_followup_422_when_message_missing_empty_or_too_long(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )
        # Seed a trace so we exercise the 422 path, not the 409 path.
        seed = self.client.post("/api/runs/run_api/analyze")
        drain_ndjson(seed)

        self.assertEqual(
            self.client.post("/api/runs/run_api/followup", json={}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post("/api/runs/run_api/followup", json={"message": ""}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/runs/run_api/followup",
                json={"message": "x" * 2001},
            ).status_code,
            422,
        )

    def test_followup_first_event_is_user_message_with_next_turn(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )
        initial = self.client.post("/api/runs/run_api/analyze")
        drain_ndjson(initial)
        self.assertEqual(initial.status_code, 200)

        response = self.client.post(
            "/api/runs/run_api/followup", json={"message": "Anything else?"}
        )

        events, _ = drain_ndjson(response)
        self.assertGreater(len(events), 0)
        first = events[0]
        self.assertEqual(first["type"], "user_message")
        self.assertEqual(first["turn"], 1)

    def test_followup_event_seqs_start_at_prior_max_plus_one(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )
        initial = self.client.post("/api/runs/run_api/analyze")
        drain_ndjson(initial)

        prior = storage.load_trace("run_api")
        self.assertIsNotNone(prior)
        prior_max_seq = max(event.seq for event in prior.events)

        response = self.client.post(
            "/api/runs/run_api/followup", json={"message": "Anything else?"}
        )
        events, _ = drain_ndjson(response)

        self.assertGreater(len(events), 0)
        self.assertEqual(events[0]["seq"], prior_max_seq + 1)

    def test_followup_done_replaces_eval_case_draft(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_early_terminated_001",
                failure_type="early_terminated",
                summary="Agent stopped early.",
            )
        )

        initial_resp = self.client.post("/api/runs/run_api/analyze")
        _, initial_terminal = drain_ndjson(initial_resp)
        initial_draft = initial_terminal["eval_case_draft"]
        self.assertIsNotNone(initial_draft)

        followup_script = [
            _tool_message(
                "search_failure_memory", {"query": "early_terminated", "top_k": 1}
            ),
            _tool_message(
                "propose_eval_case",
                _proposal_args(
                    failure_type="early_terminated",
                    retrieved_context_ids=["fm_early_terminated_001"],
                ),
            ),
        ]
        with mock.patch.object(
            eval_agent_graph,
            "_default_llm_client",
            _patched_default_llm(followup_script),
        ):
            response = self.client.post(
                "/api/runs/run_api/followup", json={"message": "Revise"}
            )

        _, terminal = drain_ndjson(response)
        new_draft = terminal["eval_case_draft"]
        self.assertIsNotNone(new_draft)
        self.assertEqual(new_draft["failure_type"], "early_terminated")
        self.assertNotEqual(new_draft["failure_type"], initial_draft["failure_type"])

    def test_followup_preserves_user_intent_and_selected_step(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )
        # Per-step analyze was removed; the deprecated /steps/{i}/analyze
        # endpoint now ignores step_index and routes to the same full-run
        # analyze path. New traces always carry selected_step=None.
        initial = self.client.post("/api/runs/run_api/steps/0/analyze")
        drain_ndjson(initial)

        followup = self.client.post("/api/runs/run_api/followup", json={"message": "Check again"})
        drain_ndjson(followup)

        trace = storage.load_trace("run_api")
        self.assertEqual(trace.user_intent, "analyze_run")
        self.assertIsNone(trace.selected_step)

    def test_followup_budget_is_FOLLOWUP_BUDGET_via_api(self) -> None:
        _write_real_png("run_api")
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )
        initial = self.client.post("/api/runs/run_api/analyze")
        drain_ndjson(initial)

        # One past the limit must trip budget_exceeded.
        followup_script = [
            _tool_message(
                "get_step_detail",
                {"run_id": "run_api", "step_index": 1, "image_detail": "high"},
            )
            for _ in range(eval_agent_graph.FOLLOWUP_BUDGET + 1)
        ]
        with mock.patch.object(
            eval_agent_graph,
            "_default_llm_client",
            _patched_default_llm(followup_script),
        ):
            response = self.client.post(
                "/api/runs/run_api/followup", json={"message": "Spend more tools"}
            )

        _, terminal = drain_ndjson(response)
        self.assertEqual(terminal["type"], "done")
        self.assertEqual(terminal["agent_trace"]["terminated_by"], "budget_exceeded")
        self.assertIsNone(terminal["eval_case_draft"])

    def test_failure_memory_search_endpoint_returns_schema_valid_results(self) -> None:
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
                tags=["constraint"],
            )
        )

        response = self.client.get(
            "/api/failure-memory/search", params={"q": "constraint", "top_k": 3}
        )

        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertGreater(len(items), 0)
        for item in items:
            FailureMemoryCase.model_validate(item)

    def test_eval_cases_search_endpoint_returns_schema_valid_results(self) -> None:
        case = sample_eval_case("ec_run_api_step_0", source_run_id="run_api")
        self.client.post("/api/eval-cases", json=case.model_dump(mode="json"))

        response = self.client.get(
            "/api/eval-cases/search", params={"q": "early terminated", "top_k": 3}
        )

        self.assertEqual(response.status_code, 200)
        items = response.json()
        self.assertGreater(len(items), 0)
        for item in items:
            EvalCase.model_validate(item)


if __name__ == "__main__":
    unittest.main()
