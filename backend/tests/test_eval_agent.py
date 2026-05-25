from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import eval_agent_graph, preprocess, rag, storage
from backend.app.eval_agent_graph import AIMessage
from backend.app.main import app
from backend.app.schemas import (
    AgentTrace,
    EvalCase,
    FailureMemoryCase,
    StepAction,
    StepObservation,
    StepResult,
    TrajectoryRun,
    TrajectoryStep,
)
from backend.tests.test_storage import sample_run


class ScriptedLLM:
    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.invocations = 0

    def invoke(self, messages: list) -> object:
        if self.invocations >= len(self.messages):
            return AIMessage(content="no more scripted messages")
        message = self.messages[self.invocations]
        self.invocations += 1
        return message


class RaisingLLM:
    def __init__(self, message: str) -> None:
        self.message = message

    def invoke(self, messages: list) -> object:
        raise RuntimeError(self.message)


def _tool_message(name: str, args: dict) -> object:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def _proposal_args(
    *,
    failure_type: str = "missed_constraint",
    retrieved_context_ids: list[str] | None = None,
) -> dict:
    retrieved_context_ids = retrieved_context_ids if retrieved_context_ids is not None else ["fm_missed_constraint_001"]
    evidence = [
        {
            "claim": "Step 0 was inspected.",
            "source": "trajectory",
            "run_id": "run_1",
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
        "run_id": "run_1",
        "failure_step": 0,
        "failure_type": failure_type,
        "expected_behavior": "The agent should satisfy the task constraint.",
        "actual_behavior": "The trajectory does not show the constraint being satisfied.",
        "evidence": evidence,
        "regression_rule": "Check the constraint before completing the task.",
        "retrieved_context_ids": retrieved_context_ids,
    }


def _happy_script() -> list:
    return [
        _tool_message("get_run", {"run_id": "run_1"}),
        _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 0, "image_detail": "high"}),
        _tool_message("search_failure_memory", {"query": "missed_constraint", "top_k": 1}),
        _tool_message("propose_eval_case", _proposal_args()),
    ]


def _attach_tiny_png(run_id: str, filename: str = "screenshot_001.png") -> None:
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(buf, format="PNG")
    storage.save_screenshots(run_id, {filename: buf.getvalue()})


def _payload_has_forbidden_image_key(payload: object) -> bool:
    forbidden = {"screenshot_bytes", "image_bytes", "image_data"}
    if isinstance(payload, dict):
        if forbidden.intersection(payload.keys()):
            return True
        return any(_payload_has_forbidden_image_key(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_has_forbidden_image_key(item) for item in payload)
    return False


class EvalAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_env = {
            "TRAJECTA_DATA_DIR": os.environ.get("TRAJECTA_DATA_DIR"),
            "TRAJECTA_CHROMA_DIR": os.environ.get("TRAJECTA_CHROMA_DIR"),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "TRAJECTA_AGENT_MODEL": os.environ.get("TRAJECTA_AGENT_MODEL"),
            "TRAJECTA_VLM_MODEL": os.environ.get("TRAJECTA_VLM_MODEL"),
        }
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("TRAJECTA_AGENT_MODEL", None)
        os.environ.pop("TRAJECTA_VLM_MODEL", None)
        rag._client_cache = None
        rag._embedding_cache = None

        storage.save_run(sample_run("run_1", status="failed"))
        storage.save_run(sample_run("success_run", status="success"))
        rag.upsert_successful_run(sample_run("success_run", status="success"))
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="The agent ignored a user constraint.",
                fix_hint="Re-check constraints before completion.",
                tags=["constraint"],
            )
        )

    def tearDown(self) -> None:
        rag._client_cache = None
        rag._embedding_cache = None
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_happy_path_produces_valid_trace(self) -> None:
        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        AgentTrace.model_validate(result.trace.model_dump(mode="json"))
        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        draft = EvalCase.model_validate(result.eval_case_draft)
        self.assertFalse(draft.human_validated)

    def test_trace_seq_is_strictly_monotonic(self) -> None:
        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        seqs = [event.seq for event in result.trace.events]
        turns = [event.turn for event in result.trace.events]
        self.assertEqual(seqs, list(range(len(seqs))))
        self.assertEqual(turns, sorted(turns))

    def test_stream_yields_events_before_done(self) -> None:
        stream = eval_agent_graph.stream_analyze_run(
            "run_1",
            llm_client=ScriptedLLM(_happy_script()),
            persist=False,
        )

        first = next(stream)

        self.assertIsInstance(first, eval_agent_graph.AgentTraceEvent)
        self.assertEqual(first.type, "agent_message")

    def test_budget_exceeded_terminates_turn(self) -> None:
        script = [
            _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 0, "image_detail": "high"})
            for _ in range(9)
        ]

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.terminated_by, "budget_exceeded")
        self.assertGreater(len(result.errors), 0)
        self.assertIsNone(result.eval_case_draft)

    def test_budget_counts_only_budgeted_tools(self) -> None:
        script = [
            _tool_message("get_run", {"run_id": "run_1"}),
            _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 0, "image_detail": "high"}),
            _tool_message("get_run", {"run_id": "run_1"}),
            _tool_message("search_failure_memory", {"query": "missed_constraint", "top_k": 1}),
            _tool_message("propose_eval_case", _proposal_args()),
        ]

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.tool_call_count, 2)
        self.assertEqual(result.trace.terminated_by, "propose_eval_case")

    def test_propose_eval_case_is_terminal(self) -> None:
        script = [
            _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[])),
            _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 0, "image_detail": "high"}),
        ]
        llm = ScriptedLLM(script)

        result = eval_agent_graph.analyze_run("run_1", llm_client=llm)

        self.assertEqual(llm.invocations, 1)
        self.assertFalse(
            any(event.name == "get_step_detail" for event in result.trace.events),
            "terminal propose_eval_case must not fall through to later tools",
        )
        self.assertEqual(result.trace.events[-1].type, "tool_result")
        self.assertEqual(result.trace.events[-1].name, "propose_eval_case")
        self.assertEqual(result.trace.terminated_by, "propose_eval_case")

    def test_propose_eval_case_validation_error_terminates(self) -> None:
        result = eval_agent_graph.analyze_run(
            "run_1",
            llm_client=ScriptedLLM(
                [_tool_message("propose_eval_case", _proposal_args(failure_type="INVALID-TYPE", retrieved_context_ids=[]))]
            ),
        )

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertGreater(len(result.errors), 0)
        self.assertTrue(any(event.type == "tool_error" for event in result.trace.events))

    def test_malformed_tool_call_arguments_terminate_with_trace_error(self) -> None:
        # langchain-core 0.3.x rejects the raw OpenAI {"function": {...}} shape
        # in AIMessage.tool_calls, so we route the malformed call through
        # additional_kwargs — that's where real OpenAI raw responses land too
        # and _extract_tool_calls reads it as a fallback.
        bad_message = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_bad_json",
                        "function": {"name": "get_run", "arguments": "{"},
                    }
                ]
            },
        )

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM([bad_message]))

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertIsNone(result.eval_case_draft)
        self.assertTrue(any("invalid tool call from agent" in error for error in result.errors))
        self.assertTrue(
            any(
                event.type == "tool_error" and event.error and "invalid tool call from agent" in event.error
                for event in result.trace.events
            )
        )

    def test_missing_tool_call_name_terminates_with_trace_error(self) -> None:
        bad_message = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_missing_name",
                        "function": {"arguments": "{}"},
                    }
                ]
            },
        )

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM([bad_message]))

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertIsNone(result.eval_case_draft)
        self.assertTrue(any("missing a tool name" in error for error in result.errors))
        self.assertTrue(
            any(
                event.type == "tool_error" and event.error and "missing a tool name" in event.error
                for event in result.trace.events
            )
        )

    def test_agent_stops_without_tool_call_records_trace_error(self) -> None:
        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM([AIMessage(content="done")]))

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertIsNone(result.eval_case_draft)
        self.assertIn("agent stopped without calling propose_eval_case", result.errors)
        self.assertTrue(
            any(
                event.type == "tool_error"
                and event.error == "agent stopped without calling propose_eval_case"
                for event in result.trace.events
            )
        )

    def test_retrieved_context_ids_must_appear_in_trace(self) -> None:
        result = eval_agent_graph.analyze_run(
            "run_1",
            llm_client=ScriptedLLM(
                [_tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=["fm_nonexistent_999"]))]
            ),
        )

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertIn("fm_nonexistent_999", " ".join(result.errors))

    def test_nonterminal_tool_error_is_returned_to_model(self) -> None:
        script = [
            _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 99, "image_detail": "high"}),
            _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[])),
        ]

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        self.assertEqual(result.errors, [])
        self.assertTrue(any(event.type == "tool_error" and event.name == "get_step_detail" for event in result.trace.events))

    def test_evidence_context_id_must_appear_in_trace_retrieval(self) -> None:
        """docs/eval_agent.md L235: an EvidenceItem with source=failure_memory
        or eval_case must carry a context_id that appears in some prior
        search_* tool_result of the same trace. A draft that cites a
        fabricated context_id in evidence must terminate as 'error', even if
        retrieved_context_ids itself is empty.
        """

        args = _proposal_args(retrieved_context_ids=[])
        args["evidence"].append(
            {
                "claim": "The agent referenced a memory context in evidence only.",
                "source": "failure_memory",
                "context_id": "fm_nonexistent_999",
            }
        )

        result = eval_agent_graph.analyze_run(
            "run_1",
            llm_client=ScriptedLLM([_tool_message("propose_eval_case", args)]),
        )

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertIsNone(result.eval_case_draft)
        self.assertIn("fm_nonexistent_999", " ".join(result.errors))
        self.assertTrue(
            any(
                event.type == "tool_error" and event.name == "propose_eval_case"
                for event in result.trace.events
            )
        )

    def test_evidence_with_missing_context_id_for_contextual_source_terminates(self) -> None:
        """An evidence item declared source=failure_memory but with no
        context_id is unsupported evidence — the terminal call must fail."""

        args = _proposal_args(retrieved_context_ids=[])
        args["evidence"].append(
            {
                "claim": "Some failure-memory-shaped claim with no citation.",
                "source": "failure_memory",
                # context_id intentionally omitted
            }
        )

        result = eval_agent_graph.analyze_run(
            "run_1",
            llm_client=ScriptedLLM([_tool_message("propose_eval_case", args)]),
        )

        self.assertEqual(result.trace.terminated_by, "error")
        self.assertIsNone(result.eval_case_draft)
        self.assertIn("context_id", " ".join(result.errors))

    def test_trace_persisted_to_disk(self) -> None:
        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        persisted = storage.load_trace("run_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.model_dump(mode="json"), result.trace.model_dump(mode="json"))

    def test_analyze_graph_exception_persists_trace_error(self) -> None:
        events = []

        with self.assertRaisesRegex(RuntimeError, "llm crashed"):
            for item in eval_agent_graph.stream_analyze_run("run_1", llm_client=RaisingLLM("llm crashed")):
                events.append(item)

        persisted = storage.load_trace("run_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.terminated_by, "error")
        self.assertTrue(events)
        self.assertEqual(persisted.events[-1].type, "tool_error")
        self.assertEqual(persisted.events[-1].name, "graph_execution")
        self.assertIn("llm crashed", persisted.events[-1].error or "")

    def test_analyze_step_uses_selected_step(self) -> None:
        result = eval_agent_graph.analyze_step("run_1", 0, llm_client=ScriptedLLM(_happy_script()))

        self.assertEqual(result.trace.user_intent, "analyze_step")
        self.assertEqual(result.trace.selected_step, 0)

    def test_followup_increments_turn(self) -> None:
        initial = eval_agent_graph.analyze_step("run_1", 0, llm_client=ScriptedLLM(_happy_script()))
        initial_event_count = len(initial.trace.events)

        result = eval_agent_graph.followup(
            "run_1",
            "Check step 2",
            llm_client=ScriptedLLM([_tool_message("propose_eval_case", _proposal_args())]),
        )

        self.assertEqual(result.trace.turn_count, 2)
        self.assertTrue(result.trace.events[initial_event_count:])
        self.assertTrue(all(event.turn == 1 for event in result.trace.events[initial_event_count:]))
        self.assertEqual(result.trace.user_intent, "analyze_step")
        self.assertEqual(result.trace.selected_step, 0)

    def test_followup_without_prior_trace_returns_409(self) -> None:
        client = TestClient(app)

        response = client.post("/api/runs/run_1/followup", json={"message": "Check step 2"})

        self.assertEqual(response.status_code, 409)

    def test_followup_budget_is_4(self) -> None:
        initial = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))
        initial_events = [event.model_dump(mode="json") for event in initial.trace.events]
        script = [
            _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 0, "image_detail": "high"})
            for _ in range(5)
        ]

        result = eval_agent_graph.followup("run_1", "Spend more tools", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.terminated_by, "budget_exceeded")
        self.assertEqual(
            [event.model_dump(mode="json") for event in result.trace.events[: len(initial_events)]],
            initial_events,
        )

    def test_followup_graph_exception_persists_user_message_and_trace_error(self) -> None:
        initial = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))
        initial_event_count = len(initial.trace.events)
        events = []

        with self.assertRaisesRegex(RuntimeError, "followup crashed"):
            for item in eval_agent_graph.stream_followup(
                "run_1",
                "Try again",
                llm_client=RaisingLLM("followup crashed"),
            ):
                events.append(item)

        persisted = storage.load_trace("run_1")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.terminated_by, "error")
        self.assertEqual(persisted.turn_count, 2)
        self.assertGreaterEqual(len(events), 2)
        new_events = persisted.events[initial_event_count:]
        self.assertEqual(new_events[0].type, "user_message")
        self.assertEqual(new_events[0].message, "Try again")
        self.assertEqual(new_events[-1].type, "tool_error")
        self.assertEqual(new_events[-1].name, "graph_execution")
        self.assertIn("followup crashed", new_events[-1].error or "")

    def test_fallback_recursion_limit_applies_inside_tool_call_batch(self) -> None:
        old_state_graph = eval_agent_graph.StateGraph
        eval_agent_graph.StateGraph = None
        tool_calls = [
            {"name": "get_run", "args": {"run_id": "run_1"}, "id": f"call_get_run_{index}"}
            for index in range(40)
        ]
        try:
            with self.assertRaisesRegex(RuntimeError, "agent graph exceeded recursion limit"):
                for _item in eval_agent_graph.stream_analyze_run(
                    "run_1",
                    llm_client=ScriptedLLM([AIMessage(content="", tool_calls=tool_calls)]),
                    budget=1,
                ):
                    pass
        finally:
            eval_agent_graph.StateGraph = old_state_graph

        persisted = storage.load_trace("run_1")
        self.assertIsNotNone(persisted)
        completed_get_run_results = [
            event for event in persisted.events if event.type == "tool_result" and event.name == "get_run"
        ]
        self.assertLess(len(completed_get_run_results), len(tool_calls))
        self.assertEqual(persisted.events[-1].type, "tool_error")
        self.assertEqual(persisted.events[-1].name, "graph_execution")

    def test_no_screenshot_bytes_in_trace(self) -> None:
        _attach_tiny_png("run_1")

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        for event in result.trace.events:
            self.assertFalse(_payload_has_forbidden_image_key(event.args or {}))
            self.assertFalse(_payload_has_forbidden_image_key(event.result or {}))

    def test_offline_mock_agent_produces_valid_trace(self) -> None:
        result = eval_agent_graph.analyze_run("run_1")

        AgentTrace.model_validate(result.trace.model_dump(mode="json"))
        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        EvalCase.model_validate(result.eval_case_draft)

    def test_step_detail_evidence_carries_matching_trace_event_seq(self) -> None:
        """docs/eval_agent.md L236: EvidenceItem with source in
        {step_detail_high, step_detail_low} should carry trace_event_seq
        pointing at a get_step_detail tool_result with the same seq.
        """

        proposal_args = {
            "run_id": "run_1",
            "failure_step": 0,
            "failure_type": "missed_constraint",
            "expected_behavior": "The agent should satisfy the constraint.",
            "actual_behavior": "The trajectory does not show the constraint satisfied.",
            "evidence": [
                {
                    "claim": "Step 0 was inspected at high detail.",
                    "source": "step_detail_high",
                    "run_id": "run_1",
                    "step_index": 0,
                    # trace_event_seq filled in dynamically below by the test
                    # via a scripted message rewrite — but the ScriptedLLM is
                    # static, so we instead build the script such that the
                    # known seq of the high-detail tool_result is predictable.
                    "trace_event_seq": 2,
                }
            ],
            "regression_rule": "Verify the constraint before finishing the task.",
            "retrieved_context_ids": [],
        }
        script = [
            _tool_message(
                "get_step_detail",
                {"run_id": "run_1", "step_index": 0, "image_detail": "high"},
            ),
            _tool_message("propose_eval_case", proposal_args),
        ]

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        get_step_detail_results = [
            event
            for event in result.trace.events
            if event.type == "tool_result" and event.name == "get_step_detail"
        ]
        self.assertEqual(len(get_step_detail_results), 1)
        expected_seq = get_step_detail_results[0].seq
        for item in result.eval_case_draft["evidence"]:
            if item.get("source") in {"step_detail_high", "step_detail_low"}:
                self.assertEqual(item.get("trace_event_seq"), expected_seq)

    def test_offline_mock_get_step_detail_count_bounded(self) -> None:
        """docs/testing.md "agent uses get_step_detail no more than
        min(tool_call_budget, ceil(0.3 * step_count)) times on run-level
        analysis." The offline mock calls get_step_detail at most once,
        which trivially satisfies the bound for any step_count.
        """

        steps = [
            TrajectoryStep(
                index=i,
                observation=StepObservation(screenshot=f"screenshot_{i:03d}.png"),
                action=StepAction(type="wait", raw="wait()"),
                result=StepResult(status="failed" if i == 5 else "unknown"),
            )
            for i in range(30)
        ]
        run = TrajectoryRun(run_id="run_big", task="Find a result", status="failed", steps=steps)
        storage.save_run(run)
        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_missed_constraint_001",
                failure_type="missed_constraint",
                summary="Constraint missed.",
            )
        )

        result = eval_agent_graph.analyze_run("run_big")

        step_detail_calls = sum(
            1
            for event in result.trace.events
            if event.type == "tool_call" and event.name == "get_step_detail"
        )
        bound = min(eval_agent_graph.INITIAL_BUDGET, math.ceil(0.3 * 30))
        self.assertLessEqual(step_detail_calls, bound)

    def test_followup_does_not_invoke_preprocess_node(self) -> None:
        """docs/testing.md: a follow-up turn re-resumes the loop from the
        persisted messages and does not invoke the preprocess node again.
        """

        eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        with mock.patch.object(
            preprocess,
            "load_or_build_digest",
            wraps=preprocess.load_or_build_digest,
        ) as spy:
            eval_agent_graph.followup(
                "run_1",
                "Anything else?",
                llm_client=ScriptedLLM([_tool_message("propose_eval_case", _proposal_args())]),
            )

        spy.assert_not_called()

    def test_followup_two_propose_eval_case_latest_defines_draft(self) -> None:
        """A follow-up turn that calls propose_eval_case produces a new draft;
        the trace contains two propose_eval_case tool calls and the latest one
        defines the current draft.
        """

        rag.upsert_failure_memory(
            FailureMemoryCase(
                case_id="fm_early_terminated_001",
                failure_type="early_terminated",
                summary="The agent stopped early.",
            )
        )

        initial = eval_agent_graph.analyze_run(
            "run_1", llm_client=ScriptedLLM(_happy_script())
        )
        self.assertEqual(initial.eval_case_draft["failure_type"], "missed_constraint")

        followup_args = _proposal_args(
            failure_type="early_terminated",
            retrieved_context_ids=["fm_early_terminated_001"],
        )
        result = eval_agent_graph.followup(
            "run_1",
            "Revise to early_terminated",
            llm_client=ScriptedLLM(
                [
                    _tool_message("search_failure_memory", {"query": "early_terminated", "top_k": 1}),
                    _tool_message("propose_eval_case", followup_args),
                ]
            ),
        )

        propose_calls = [
            event
            for event in result.trace.events
            if event.type == "tool_call" and event.name == "propose_eval_case"
        ]
        self.assertEqual(len(propose_calls), 2)
        self.assertEqual(propose_calls[-1].args["failure_type"], "early_terminated")
        self.assertEqual(result.eval_case_draft["failure_type"], "early_terminated")

    def test_retrieved_context_ids_resolved_across_turns(self) -> None:
        """docs/eval_agent.md L181-182: EvalCase.retrieved_context_ids returned
        by a follow-up propose_eval_case may reference search results from
        any earlier turn — the whole trace is the evidence pool.
        """

        initial_args = _proposal_args(retrieved_context_ids=["fm_missed_constraint_001"])
        initial = eval_agent_graph.analyze_run(
            "run_1",
            llm_client=ScriptedLLM(
                [
                    _tool_message("search_failure_memory", {"query": "missed_constraint", "top_k": 1}),
                    _tool_message("propose_eval_case", initial_args),
                ]
            ),
        )
        self.assertEqual(initial.trace.terminated_by, "propose_eval_case")

        followup_args = _proposal_args(
            failure_type="missed_constraint",
            retrieved_context_ids=["fm_missed_constraint_001"],
        )
        # The follow-up turn re-uses the context_id from the initial turn
        # without re-running search_failure_memory; the trace from turn 0
        # provides the search result.
        result = eval_agent_graph.followup(
            "run_1",
            "Reaffirm the same finding",
            llm_client=ScriptedLLM([_tool_message("propose_eval_case", followup_args)]),
        )

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        self.assertIn(
            "fm_missed_constraint_001",
            result.eval_case_draft["retrieved_context_ids"],
        )


if __name__ == "__main__":
    unittest.main()
