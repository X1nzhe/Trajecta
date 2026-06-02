from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from backend.app import prompts, rag, storage, tools
from backend.app.ids import make_eval_case_id
from backend.app.schemas import (
    EvalCase,
    EvidenceItem,
    FailureMemoryCase,
    StepDigest,
    TrajectoryDigest,
)
from backend.tests.test_storage import sample_run


def _tiny_png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _attach_screenshot(run_id: str, filename: str = "screenshot_001.png") -> None:
    storage.save_screenshots(run_id, {filename: _tiny_png_bytes()})


class ToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        # conftest already isolates TRAJECTA_DATA_DIR per test; we only need to
        # point Chroma inside the same temp dir so RAG state resets between tests.
        data_dir = os.environ["TRAJECTA_DATA_DIR"]
        self.previous_chroma_dir = os.environ.get("TRAJECTA_CHROMA_DIR")
        self.previous_vlm_high_detail_prompt_version = os.environ.get(
            "TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION"
        )
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(data_dir, "chroma")
        os.environ.pop("TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION", None)
        rag._client_cache = None
        rag._embedding_cache = None

    def tearDown(self) -> None:
        rag._client_cache = None
        rag._embedding_cache = None
        if self.previous_chroma_dir is None:
            os.environ.pop("TRAJECTA_CHROMA_DIR", None)
        else:
            os.environ["TRAJECTA_CHROMA_DIR"] = self.previous_chroma_dir
        if self.previous_vlm_high_detail_prompt_version is None:
            os.environ.pop("TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION", None)
        else:
            os.environ["TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION"] = (
                self.previous_vlm_high_detail_prompt_version
            )

    def test_get_run_returns_run_with_attached_digest(self) -> None:
        storage.save_run(sample_run())
        digest = TrajectoryDigest(
            run_id="run_1",
            task="Find a result",
            step_count=1,
            steps=[
                StepDigest(
                    index=0,
                    action_type="wait",
                    action_text="wait",
                    result_status="unknown",
                    coord_validation_status="unknown",
                    has_screenshot=False,
                )
            ],
            preprocess_model="mock",
        )
        storage.save_digest("run_1", digest)

        result = tools.get_run("run_1")

        self.assertEqual(result["run_id"], "run_1")
        self.assertIn("digest", result)
        validated = TrajectoryDigest.model_validate(result["digest"])
        self.assertEqual(validated.run_id, "run_1")
        self.assertEqual(validated.step_count, 1)

    def test_get_run_masks_status_in_eval_mode(self) -> None:
        """TRAJECTA_EVAL_MODE=1 forces run.status="unknown" on the returned payload.

        main.py:198 flips run.status to "success" / "failed" when a human
        validates an EvalCase. Without masking, an agent re-evaluating a
        previously-validated run would read the human's verdict straight
        off ``get_run`` and have nothing to derive. Per-step result.status
        is left alone — MolmoWeb source has no task-level assertions and
        the validation flip does not touch step rows.
        """
        storage.save_run(sample_run(status="failed"))

        with mock.patch.dict(os.environ, {"TRAJECTA_EVAL_MODE": "1"}):
            result = tools.get_run("run_1")

        self.assertEqual(result["status"], "unknown")
        # Sanity: non-status fields still come through.
        self.assertEqual(result["run_id"], "run_1")
        self.assertEqual(result["task"], "Find a result")

    def test_get_run_keeps_status_outside_eval_mode(self) -> None:
        """Product path must return the real persisted status — the UI
        deep-links from a run row to its verdict via this field."""
        storage.save_run(sample_run(status="failed"))

        env_without_eval = {k: v for k, v in os.environ.items() if k != "TRAJECTA_EVAL_MODE"}
        with mock.patch.dict(os.environ, env_without_eval, clear=True):
            result = tools.get_run("run_1")

        self.assertEqual(result["status"], "failed")

    def test_vlm_high_detail_prompt_registry_loads_default(self) -> None:
        active = prompts.active_vlm_high_detail_prompt()

        self.assertEqual(active.version, "v1_task_context")
        self.assertIn(active.version, prompts.available_vlm_high_detail_prompt_versions())
        self.assertIn("constraint_evidence:", active.text)
        self.assertEqual(len(active.sha256), 64)

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

        spy.assert_called_once_with("constraint", top_k=5, exclude_source_run_id=None)
        self.assertEqual(results[0]["case_id"], "fm_missed_constraint_001")
        # Return shape is JSON-mode dict (not Pydantic instance).
        self.assertIsInstance(results[0], dict)

    def test_search_failure_memory_passes_exclude_source_run_id(self) -> None:
        """Leakage guard: tools.search_failure_memory forwards exclude_source_run_id
        verbatim to rag.query_failure_memory. Verifies the wire-through; the
        ChromaDB-side filtering itself is covered in test_rag.py."""
        with mock.patch(
            "backend.app.tools.rag.query_failure_memory", return_value=[]
        ) as spy:
            tools.search_failure_memory("anything", top_k=3, exclude_source_run_id="run_42")

        spy.assert_called_once_with(
            "anything", top_k=3, exclude_source_run_id="run_42"
        )

    def test_search_failure_memory_redacts_source_run_id_in_eval_mode(self) -> None:
        """TRAJECTA_EVAL_MODE=1 strips source_run_id from returned cases.

        Cold-start eval state: failure_memory is seeded from cases.jsonl
        whose source_run_ids point to runs in the golden eval set. The
        agent must not be able to pivot via get_run(source_run_id) to
        look up the human exemplar for a given failure pattern.
        """
        seeded = FailureMemoryCase(
            case_id="fm_missed_constraint_001",
            failure_type="missed_constraint",
            summary="The agent ignored a user constraint.",
            tags=["constraint"],
            source_run_id="some_other_run",
        )
        with mock.patch("backend.app.tools.rag.query_failure_memory", return_value=[seeded]), \
             mock.patch.dict(os.environ, {"TRAJECTA_EVAL_MODE": "1"}):
            results = tools.search_failure_memory("constraint", top_k=1)

        self.assertEqual(results[0]["case_id"], "fm_missed_constraint_001")
        self.assertNotIn("source_run_id", results[0])
        # Non-sensitive fields must survive.
        self.assertEqual(results[0]["failure_type"], "missed_constraint")
        self.assertEqual(results[0]["summary"], "The agent ignored a user constraint.")

    def test_find_similar_successful_run_runs_normally_in_eval_mode(self) -> None:
        """Eval mode does NOT short-circuit this tool.

        The returned run_ids are task-similar success comparators for
        replay-and-diff, not labeled answers for the run under analysis.
        The exclude_run_id auto-injection handles the only real leak
        (a run being returned as similar to itself). Killing the tool
        would also kill a legitimate reasoning channel; see eval/runs
        analysis from 2026-05-28.
        """
        canned = [{"run_id": "success_run", "task": "t", "status": "success", "step_count": 3}]
        with mock.patch(
            "backend.app.tools.rag.query_similar_successful_trajectories", return_value=canned
        ) as spy, mock.patch.dict(os.environ, {"TRAJECTA_EVAL_MODE": "1"}):
            results = tools.find_similar_successful_run("t", top_k=3)

        self.assertEqual(results, canned)
        spy.assert_called_once()

    def test_search_eval_cases_filters_by_exclude_source_run_id_in_eval_mode(self) -> None:
        """Eval mode does NOT short-circuit; instead it relies on the
        dispatcher's auto-injection of exclude_source_run_id=current_run_id.

        An EvalCase whose source_run_id equals the run under analysis
        carries that run's verdict — that IS direct answer leakage. But
        cases derived from OTHER runs are legitimate precedent the agent
        should be able to retrieve.
        """
        seeded = EvalCase(
            case_id="ec_run_other_step_0",
            source_run_id="run_other",
            task="t",
            failure_step=0,
            failure_type="early_terminated",
            expected_behavior="e",
            actual_behavior="a",
            evidence=[EvidenceItem(claim="c", source="trajectory", run_id="run_other", step_index=0)],
            regression_rule="r",
            human_validated=True,
        )
        with mock.patch(
            "backend.app.tools.rag.query_failure_eval_cases", return_value=[seeded]
        ) as spy, mock.patch.dict(os.environ, {"TRAJECTA_EVAL_MODE": "1"}):
            results = tools.search_eval_cases("t", top_k=3, exclude_source_run_id="run_1")

        # Tool no longer short-circuits — it forwards to rag and returns
        # what rag returns. The filter itself is verified in test_rag.py.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["case_id"], "ec_run_other_step_0")
        spy.assert_called_once_with(
            "t", top_k=3, only_validated=True, exclude_source_run_id="run_1"
        )

    def test_propose_eval_case_schema_enforces_failure_type_enum(self) -> None:
        """propose_eval_case's OpenAI tool schema must constrain failure_type
        to the v1 vocabulary via JSON Schema ``enum``.

        Without this, OpenAI strict-mode decoding won't filter tokens and
        the model emits typos like ``early_termination`` (observed in
        Run 4: a6daae and 3672b077 both lost verdicts to this exact typo).
        The runtime ``V1_FAILURE_VOCABULARY`` check is still there as
        defense in depth, but the schema enforcement should make this path
        unreachable for strict-mode-compliant models.
        """
        from langchain_core.utils.function_calling import convert_to_openai_tool

        schema = convert_to_openai_tool(tools.propose_eval_case)
        ft = schema["function"]["parameters"]["properties"]["failure_type"]
        # Schema is anyOf[ enum-string, null ] because the field is Optional.
        enum_branch = next(
            (branch for branch in ft.get("anyOf", []) if "enum" in branch),
            None,
        )
        self.assertIsNotNone(enum_branch, f"failure_type schema missing enum branch: {ft}")
        self.assertEqual(
            set(enum_branch["enum"]),
            {
                "early_terminated",
                "wrong_target",
                "wrong_result",
                "missed_constraint",
                "inefficient_search",
            },
        )

    def test_propose_eval_case_rejects_off_vocabulary_failure_type(self) -> None:
        """Tool entry must reject failure_types outside V1_FAILURE_VOCABULARY.

        Observed in real eval runs: gpt-5.4-mini emitted
        ``wrong_destination``, ``wrong_destination_search``, and
        ``unsupported_answer``. EvalCase.failure_type is regex-shape-only,
        so the bad labels would otherwise persist. The ValueError lifts
        to the agent loop as a recoverable tool error so the agent retries
        with a vocab-compliant value.
        """
        storage.save_run(sample_run())
        with self.assertRaises(ValueError) as ctx:
            tools.propose_eval_case(
                run_id="run_1",
                failure_step=0,
                failure_type="wrong_destination",  # off-vocab
                expected_behavior="The agent should finish the task.",
                actual_behavior="The agent stopped before finishing.",
                evidence=[{"claim": "c", "source": "trajectory", "run_id": "run_1", "step_index": 0}],
                regression_rule="r",
                retrieved_context_ids=[],
            )
        self.assertIn("wrong_destination", str(ctx.exception))
        self.assertIn("v1 vocabulary", str(ctx.exception))

    def test_search_failure_memory_keeps_source_run_id_outside_eval_mode(self) -> None:
        """Without TRAJECTA_EVAL_MODE, source_run_id stays on the payload.

        Product path (UI, API) needs source_run_id to deep-link from a
        case to its origin run. Redaction must be eval-only.
        """
        seeded = FailureMemoryCase(
            case_id="fm_wrong_target_001",
            failure_type="wrong_target",
            summary="The agent acted on the wrong target.",
            tags=[],
            source_run_id="exemplar_run",
        )
        with mock.patch("backend.app.tools.rag.query_failure_memory", return_value=[seeded]), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRAJECTA_EVAL_MODE", None)
            results = tools.search_failure_memory("x", top_k=1)

        self.assertEqual(results[0]["source_run_id"], "exemplar_run")

    def test_find_similar_successful_run_delegates_to_rag(self) -> None:
        canned = [{"run_id": "success_run", "task": "Find a result", "status": "success", "step_count": 3}]
        with mock.patch("backend.app.tools.rag.query_similar_successful_trajectories", return_value=canned) as spy:
            results = tools.find_similar_successful_run("Find a result", top_k=3)

        spy.assert_called_once_with("Find a result", top_k=3, exclude_run_id=None)
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
        with mock.patch("backend.app.tools.rag.query_failure_eval_cases", return_value=[seeded]) as spy:
            results = tools.search_eval_cases("early terminated", top_k=4, only_validated=True)

        spy.assert_called_once_with(
            "early terminated", top_k=4, only_validated=True, exclude_source_run_id=None
        )
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
        self.assertIn("vlm_prompt_version", result)
        self.assertIn("vlm_prompt_sha256", result)
        self.assertIn("task_context", result)
        self.assertIn("action", result)
        self.assertIn("observation", result)
        self.assertIn("result", result)
        self.assertIn("coordinate_validation", result)
        self.assertEqual(result["image_detail"], "high")
        self.assertFalse(result["has_screenshot"])
        self.assertIsNone(result["vlm_summary"])
        self.assertIsNone(result["vlm_prompt_version"])
        self.assertIsNone(result["vlm_prompt_sha256"])
        self.assertEqual(result["task_context"]["task"], "Find a result")

    def test_get_step_detail_low_detail_mode(self) -> None:
        storage.save_run(sample_run())
        _attach_screenshot("run_1")

        result = tools.get_step_detail("run_1", step_index=0, image_detail="low")

        self.assertEqual(result["image_detail"], "low")
        self.assertTrue(result["has_screenshot"])
        self.assertIsNotNone(result["vlm_summary"])
        self.assertLessEqual(len(result["vlm_summary"]), 200)
        self.assertIsNone(result["vlm_prompt_version"])
        self.assertIsNone(result["vlm_prompt_sha256"])

    def test_get_step_detail_raw_when_spotlighting_on_without_token(self) -> None:
        """Phase 8 P0 regression: get_step_detail returns RAW data and must
        not require a spotlight token. With TRAJECTA_SPOTLIGHTING on (the
        production default) and no active token — the state on the HTTP/API
        path — it returns unwrapped fields without raising. The agent path
        wraps separately at the tool-result seam in eval_agent_graph.
        """
        storage.save_run(sample_run())
        _attach_screenshot("run_1")
        prompts.set_spotlight_token(None)

        with mock.patch.dict(os.environ, {"TRAJECTA_SPOTLIGHTING": "on"}):
            result = tools.get_step_detail("run_1", step_index=0, image_detail="high")

        self.assertIsNotNone(result["vlm_summary"])
        self.assertNotIn("<TRAJECTA_DATA_", str(result))
        self.assertEqual(result["action"]["raw"], "wait()")

    def test_get_step_detail_passes_task_context_to_high_detail_vlm(self) -> None:
        storage.save_run(sample_run())
        _attach_screenshot("run_1")
        captured: dict = {}

        class SpyVLM:
            model_name = "spy"

            def summarize_low_detail(self, *args, **kwargs):
                raise AssertionError("low detail should not be called")

            def summarize_high_detail(self, image_bytes, **kwargs):
                captured.update(kwargs)
                return "page_state: ok\nconstraint_evidence: supported"

        with mock.patch("backend.app.tools.llm.get_vlm_client", return_value=SpyVLM()):
            result = tools.get_step_detail("run_1", step_index=0, image_detail="high")

        self.assertEqual(result["vlm_summary"], "page_state: ok\nconstraint_evidence: supported")
        active = prompts.active_vlm_high_detail_prompt()
        self.assertEqual(result["vlm_prompt_version"], active.version)
        self.assertEqual(result["vlm_prompt_sha256"], active.sha256)
        self.assertEqual(captured["task"], "Find a result")
        self.assertEqual(captured["image_name"], "screenshot_001.png")
        self.assertEqual(captured["action_type"], "wait")
        self.assertEqual(captured["action_raw"], "wait()")

    def test_get_step_detail_high_detail_with_screenshot(self) -> None:
        storage.save_run(sample_run())
        _attach_screenshot("run_1")

        result = tools.get_step_detail("run_1", step_index=0, image_detail="high")

        self.assertTrue(result["has_screenshot"])
        self.assertIsNotNone(result["vlm_summary"])
        self.assertIn("constraint_evidence:", result["vlm_summary"])
        self.assertIn("task_relevant_visible_text:", result["vlm_summary"])
        active = prompts.active_vlm_high_detail_prompt()
        self.assertEqual(result["vlm_prompt_version"], active.version)
        self.assertEqual(result["vlm_prompt_sha256"], active.sha256)

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
        _attach_screenshot("run_1")

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

        self_run = sample_run("run_a", status="success")
        success_run = sample_run("run_c", status="success")
        failed_run = sample_run("run_b", status="failed")
        storage.save_run(self_run)
        storage.save_run(success_run)
        storage.save_run(failed_run)
        rag.upsert_successful_trajectory(self_run)
        rag.upsert_successful_trajectory(success_run)

        results = tools.find_similar_successful_run(
            "Find a result", top_k=5, exclude_run_id="run_a"
        )

        self.assertGreater(len(results), 0)
        run_ids = [r["run_id"] for r in results]
        self.assertNotIn("run_a", run_ids)  # self exclusion
        self.assertNotIn("run_b", run_ids)  # status filter
        self.assertIn("run_c", run_ids)
        self.assertTrue(all(r["status"] == "success" for r in results))

    def test_find_similar_successful_run_empty_when_no_success_run_indexed(self) -> None:
        """docs/testing.md: find_similar_successful_run returns an empty list
        when no successful run is indexed for the task.
        """

        results = tools.find_similar_successful_run("Find a result", top_k=3)

        self.assertEqual(results, [])

    def test_propose_eval_case_rejects_half_populated_failure_fields(self) -> None:
        """docs/contracts.md EvalCase XOR rule: a draft must populate all
        five failure fields (failure case) or none of them (success case).
        Half-populated drafts are rejected by the EvalCase model_validator.
        """

        from pydantic import ValidationError

        storage.save_run(sample_run())

        with self.assertRaises(ValidationError):
            tools.propose_eval_case(
                run_id="run_1",
                failure_step=0,
                failure_type="early_terminated",
                expected_behavior="x",
                # actual_behavior and regression_rule deliberately missing
                evidence=[{"claim": "c", "source": "trajectory", "run_id": "run_1"}],
                retrieved_context_ids=[],
            )

    def test_propose_eval_case_success_shape(self) -> None:
        """Calling propose_eval_case with no failure fields produces a
        success-shape EvalCase draft using the success case_id namespace.
        """

        storage.save_run(sample_run())

        draft = tools.propose_eval_case(
            run_id="run_1",
            evidence=[{"claim": "Step 0 reached the expected page.", "source": "trajectory", "run_id": "run_1", "step_index": 0}],
            retrieved_context_ids=[],
        )

        self.assertEqual(draft["case_id"], "ec_run_1_success")
        self.assertIsNone(draft["failure_step"])
        self.assertIsNone(draft["failure_type"])
        self.assertIsNone(draft["regression_rule"])
        self.assertFalse(draft["human_validated"])

    def test_propose_eval_case_passes_suggested_followups(self) -> None:
        """Agent-authored followup chips ride along with the terminal call.

        Transport-only: they appear in the tool's returned payload (which
        the trace event carries) but are NOT persisted into the EvalCase.
        """

        storage.save_run(sample_run())

        draft = tools.propose_eval_case(
            run_id="run_1",
            evidence=[{"claim": "Step 0 reached the expected page.", "source": "trajectory", "run_id": "run_1", "step_index": 0}],
            retrieved_context_ids=[],
            suggested_followups=[
                {"label": "Inspect step 0", "message": "Inspect step 0 in detail."},
                {"label": "Find similar", "message": "Find similar successful runs."},
            ],
        )

        self.assertIn("suggested_followups", draft)
        self.assertEqual(len(draft["suggested_followups"]), 2)
        self.assertEqual(draft["suggested_followups"][0]["label"], "Inspect step 0")

    def test_propose_eval_case_rejects_too_many_followups(self) -> None:
        storage.save_run(sample_run())

        with self.assertRaises(ValueError):
            tools.propose_eval_case(
                run_id="run_1",
                evidence=[{"claim": "x", "source": "trajectory", "run_id": "run_1"}],
                retrieved_context_ids=[],
                suggested_followups=[
                    {"label": f"chip {i}", "message": f"msg {i}"} for i in range(5)
                ],
            )

    def test_propose_eval_case_truncates_overlong_followup_fields(self) -> None:
        """Agent occasionally emits a label/message that's one or two
        characters over the FollowupSuggestion limit. Rejecting the
        whole proposal for a cosmetic UI chip is the wrong tradeoff —
        the chip is transport-only and a truncated version is still
        usable. Truncate silently; the verdict still lands.
        """

        storage.save_run(sample_run())

        draft = tools.propose_eval_case(
            run_id="run_1",
            evidence=[{"claim": "x", "source": "trajectory", "run_id": "run_1"}],
            retrieved_context_ids=[],
            suggested_followups=[
                {"label": "x" * 41, "message": "ok"},
                {"label": "fine", "message": "y" * 250},
            ],
        )

        self.assertIn("suggested_followups", draft)
        self.assertEqual(len(draft["suggested_followups"]), 2)
        # Label truncated to 40, message truncated to 200.
        self.assertEqual(len(draft["suggested_followups"][0]["label"]), 40)
        self.assertEqual(len(draft["suggested_followups"][1]["message"]), 200)

    def test_propose_eval_case_drops_unrecoverable_followup_shapes(self) -> None:
        """Items that aren't dicts, or whose label/message aren't
        strings, or that collapse to empty after strip, are silently
        dropped rather than raised. Valid items in the same list still
        come through.
        """

        storage.save_run(sample_run())

        draft = tools.propose_eval_case(
            run_id="run_1",
            evidence=[{"claim": "x", "source": "trajectory", "run_id": "run_1"}],
            retrieved_context_ids=[],
            suggested_followups=[
                "not a dict",  # type: ignore[list-item]
                {"label": "", "message": "ok"},
                {"label": "  ", "message": "ok"},
                {"label": "valid", "message": "valid"},
            ],
        )

        self.assertIn("suggested_followups", draft)
        self.assertEqual(len(draft["suggested_followups"]), 1)
        self.assertEqual(draft["suggested_followups"][0]["label"], "valid")


if __name__ == "__main__":
    unittest.main()
