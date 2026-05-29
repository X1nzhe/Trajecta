from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import eval_agent_graph, preprocess, prompts, rag, storage
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
            "TRAJECTA_PROMPT_VERSION": os.environ.get("TRAJECTA_PROMPT_VERSION"),
        }
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name
        os.environ["TRAJECTA_CHROMA_DIR"] = os.path.join(self.tmp.name, "chroma")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("TRAJECTA_AGENT_MODEL", None)
        os.environ.pop("TRAJECTA_VLM_MODEL", None)
        os.environ.pop("TRAJECTA_PROMPT_VERSION", None)
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

    def test_trace_records_prompt_version_and_hash(self) -> None:
        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))
        active = prompts.active_prompt_bundle()

        self.assertEqual(result.trace.prompt_version, active.version)
        self.assertEqual(result.trace.prompt_sha256, active.sha256)
        self.assertIn(active.version, prompts.available_prompt_versions())

    def test_trace_accumulates_runtime_and_token_counts(self) -> None:
        """docs/eval_agent.md "Observability" — per-trace cost/latency
        counters live on AgentTrace so the UI can render real numbers
        for the cost ablation claim. Offline mocks have no
        usage_metadata, so token counts stay 0; runtime_ms is wall-clock
        and must be positive after any real loop iteration.
        """

        class UsageMessage(AIMessage):
            def __init__(self, name: str, args: dict, *, input_tokens: int, output_tokens: int) -> None:
                super().__init__(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])
                self.usage_metadata = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }

        script = [
            UsageMessage("get_run", {"run_id": "run_1"}, input_tokens=120, output_tokens=18),
            UsageMessage("propose_eval_case", _proposal_args(retrieved_context_ids=[]), input_tokens=240, output_tokens=64),
        ]

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertEqual(result.trace.input_tokens, 360)
        self.assertEqual(result.trace.output_tokens, 82)
        # Wall-clock runtime is real time — just assert it's been set.
        self.assertGreaterEqual(result.trace.runtime_ms, 0)

    def test_trace_turn_metrics_split_initial_and_followup(self) -> None:
        """The UI needs per-turn costs (latest turn for the footer,
        turn 0 for the collapsed-trace header) so neither display keeps
        growing across followups. turn_metrics must carry the same
        runtime/token deltas split by ``turn``.
        """

        class UsageMessage(AIMessage):
            def __init__(self, name: str, args: dict, *, input_tokens: int, output_tokens: int) -> None:
                super().__init__(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])
                self.usage_metadata = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }

        analyze_script = [
            UsageMessage("get_run", {"run_id": "run_1"}, input_tokens=100, output_tokens=10),
            UsageMessage("propose_eval_case", _proposal_args(retrieved_context_ids=[]), input_tokens=200, output_tokens=20),
        ]
        eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(analyze_script))

        followup_script = [
            UsageMessage("propose_eval_case", _proposal_args(retrieved_context_ids=[]), input_tokens=50, output_tokens=5),
        ]
        result = eval_agent_graph.followup("run_1", "any clarification", llm_client=ScriptedLLM(followup_script))

        # Cumulative on the AgentTrace stays the way PROJECT.md's cost
        # ablation reads it — sum of every turn.
        self.assertEqual(result.trace.input_tokens, 350)
        self.assertEqual(result.trace.output_tokens, 35)

        # Per-turn split. turn 0 = initial analyze; turn 1 = followup.
        per_turn = {entry.turn: entry for entry in result.trace.turn_metrics}
        self.assertEqual(set(per_turn.keys()), {0, 1})
        self.assertEqual(per_turn[0].input_tokens, 300)
        self.assertEqual(per_turn[0].output_tokens, 30)
        self.assertEqual(per_turn[1].input_tokens, 50)
        self.assertEqual(per_turn[1].output_tokens, 5)
        # Wall-clock recorded against each turn separately too.
        self.assertGreaterEqual(per_turn[0].runtime_ms, 0)
        self.assertGreaterEqual(per_turn[1].runtime_ms, 0)

    def test_trace_token_counts_stay_zero_when_usage_metadata_missing(self) -> None:
        """Offline / mock LLM path has no usage_metadata; the trace should
        still validate with zeroed counters rather than crashing the loop.
        """

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        self.assertEqual(result.trace.input_tokens, 0)
        self.assertEqual(result.trace.output_tokens, 0)
        self.assertGreaterEqual(result.trace.runtime_ms, 0)

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

        # The happy-path script emits tool-only AIMessages (no text content),
        # so no agent_message event is produced. First event is either the
        # preprocess "phase" event (cache miss, emitted before the graph
        # runs) or the first tool_call. agent_message would also be valid
        # if a script ever produces text first.
        self.assertIsInstance(first, eval_agent_graph.AgentTraceEvent)
        self.assertIn(first.type, {"agent_message", "tool_call", "phase"})

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

    def test_malformed_tool_call_recovers_via_retry(self) -> None:
        """Malformed tool_calls payloads no longer terminate the trace.
        The diagnostic lands as a tool_error event and a HumanMessage
        nudge is appended; the agent gets another LLM call to fix its
        output. Here the second scripted reply is a valid
        propose_eval_case, so the trace terminates cleanly.
        """

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
        good_message = _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[]))

        result = eval_agent_graph.analyze_run(
            "run_1", llm_client=ScriptedLLM([bad_message, good_message])
        )

        # Recovered — terminated cleanly via propose_eval_case.
        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        # Diagnostic for the bad message is still recorded for
        # observability.
        self.assertTrue(
            any(
                event.type == "tool_error" and event.error and "invalid tool call from agent" in event.error
                for event in result.trace.events
            )
        )

    def test_missing_tool_call_name_recovers_via_retry(self) -> None:
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
        good_message = _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[]))

        result = eval_agent_graph.analyze_run(
            "run_1", llm_client=ScriptedLLM([bad_message, good_message])
        )

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
        self.assertTrue(
            any(
                event.type == "tool_error" and event.error and "missing a tool name" in event.error
                for event in result.trace.events
            )
        )

    def test_agent_stops_without_tool_call_recovers_via_retry(self) -> None:
        """When the LLM replies with plain text on turn 0, the loop
        nudges it with a HumanMessage reminding that propose_eval_case
        is required, then re-invokes. Recovery via a good follow-up
        scripted reply ends the trace cleanly.
        """

        plain_text = AIMessage(content="I think this run was successful but I'll stop here.")
        good_message = _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[]))

        result = eval_agent_graph.analyze_run(
            "run_1", llm_client=ScriptedLLM([plain_text, good_message])
        )

        self.assertEqual(result.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(result.eval_case_draft)
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

    def test_retrieved_context_ids_rejects_similar_run_run_id_with_specific_error(self) -> None:
        """A common agent mistake is quoting a run_id from
        find_similar_successful_run in retrieved_context_ids. The validator
        should still reject it (run_ids are not case_ids per
        docs/contracts.md L332), but produce a pedagogical error message
        so the next retry actually drops the bad ID instead of looping.
        """

        script = [
            _tool_message(
                "find_similar_successful_run",
                {"task": "find: answer to question", "top_k": 3, "exclude_run_id": "run_1"},
            ),
            _tool_message(
                "propose_eval_case",
                _proposal_args(retrieved_context_ids=["success_run"]),
            ),
        ]

        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(result.trace.terminated_by, "error")
        joined = " ".join(result.errors)
        self.assertIn("success_run", joined)
        # The specific error should call out the run_id confusion so the
        # agent's retry knows what to drop.
        self.assertIn("find_similar_successful_run", joined)

    def test_find_similar_successful_run_injects_exclude_run_id(self) -> None:
        """Server-side leakage guard for the replay-and-diff retrieval path.

        The LLM may emit ``find_similar_successful_run`` without
        ``exclude_run_id``. The dispatcher must force-inject the current
        ``run_id`` regardless, to prevent the agent from "rediscovering" a
        golden-set sample that is itself in the ``successful_runs`` collection
        (e.g. a previously human-validated success EvalCase promoted there).
        Symmetric to the ``search_failure_memory`` / ``exclude_source_run_id``
        guard verified elsewhere.
        """
        captured: dict = {}
        real = eval_agent_graph._TOOL_REGISTRY["find_similar_successful_run"]

        def spy(**kwargs):
            captured.update(kwargs)
            return []

        script = [
            # LLM intentionally OMITS exclude_run_id from the args.
            _tool_message("find_similar_successful_run", {"task": "any", "top_k": 3}),
            _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[])),
        ]
        with mock.patch.dict(
            eval_agent_graph._TOOL_REGISTRY,
            {"find_similar_successful_run": spy},
        ):
            eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(captured.get("exclude_run_id"), "run_1")
        # And the other args the LLM did emit must still pass through.
        self.assertEqual(captured.get("task"), "any")
        self.assertEqual(captured.get("top_k"), 3)
        # restore registry value not strictly needed (mock.patch.dict undoes
        # the patch on context exit); ``real`` referenced to silence lint.
        del real

    def test_search_eval_cases_injects_exclude_source_run_id(self) -> None:
        """Dispatcher auto-injects exclude_source_run_id=current_run_id.

        Mirrors the search_failure_memory guard. A validated EvalCase
        whose source_run_id is the run currently under analysis carries
        that run's verdict — that IS direct answer leakage. The eval-mode
        short-circuit was previously a sledgehammer for this; we now do
        surgical exclusion at the chroma layer so the agent still gets
        precedent from OTHER runs.
        """
        captured: dict = {}

        def spy(**kwargs):
            captured.update(kwargs)
            return []

        script = [
            # LLM omits exclude_source_run_id.
            _tool_message("search_eval_cases", {"query": "any", "top_k": 3}),
            _tool_message("propose_eval_case", _proposal_args(retrieved_context_ids=[])),
        ]
        with mock.patch.dict(
            eval_agent_graph._TOOL_REGISTRY,
            {"search_eval_cases": spy},
        ):
            eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(script))

        self.assertEqual(captured.get("exclude_source_run_id"), "run_1")
        self.assertEqual(captured.get("query"), "any")
        self.assertEqual(captured.get("top_k"), 3)

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

    def test_new_traces_have_no_selected_step(self) -> None:
        # Per-step analyze was removed; new traces always carry
        # user_intent="analyze_run" with selected_step=None. The schema
        # still permits the int form for back-compat reading of older
        # persisted traces (see eval_agent_graph._make_initial_state).
        result = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))

        self.assertEqual(result.trace.user_intent, "analyze_run")
        self.assertIsNone(result.trace.selected_step)

    def test_followup_increments_turn(self) -> None:
        initial = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))
        initial_event_count = len(initial.trace.events)

        result = eval_agent_graph.followup(
            "run_1",
            "Check step 2",
            llm_client=ScriptedLLM([_tool_message("propose_eval_case", _proposal_args())]),
        )

        self.assertEqual(result.trace.turn_count, 2)
        self.assertTrue(result.trace.events[initial_event_count:])
        self.assertTrue(all(event.turn == 1 for event in result.trace.events[initial_event_count:]))
        # Followup preserves user_intent + selected_step from the initial
        # analyze; both remain analyze_run / None.
        self.assertEqual(result.trace.user_intent, "analyze_run")
        self.assertIsNone(result.trace.selected_step)

    def test_followup_without_prior_trace_returns_409(self) -> None:
        client = TestClient(app)

        response = client.post("/api/runs/run_1/followup", json={"message": "Check step 2"})

        self.assertEqual(response.status_code, 409)

    def test_followup_plain_text_answer_does_not_error_or_wipe_draft(self) -> None:
        """Per the followup system prompt, the agent MAY answer a
        clarification question in plain text without invoking any tool.
        That termination is legitimate — the previous turn's draft must
        survive and terminated_by must not flip to 'error'.

        Regression for the bug where every turn-end without a tool call
        was treated as an error, silently destroying the draft on
        clarification followups.
        """

        initial = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))
        self.assertEqual(initial.trace.terminated_by, "propose_eval_case")
        self.assertIsNotNone(initial.eval_case_draft)
        initial_draft = initial.eval_case_draft

        # Plain-text reply — single AIMessage with content, no tool_calls.
        plain_reply = AIMessage(content="Here is a summary: the agent stopped early.")
        result = eval_agent_graph.followup(
            "run_1",
            "Summarize the failure memory cases.",
            llm_client=ScriptedLLM([plain_reply]),
        )

        # Turn ended cleanly without flipping to error.
        self.assertNotEqual(result.trace.terminated_by, "error")
        self.assertEqual(result.trace.turn_count, 2)
        # Draft from the initial analyze is preserved.
        self.assertEqual(result.eval_case_draft, initial_draft)
        # The agent_message event is recorded so the UI renders the answer.
        followup_agent_messages = [
            event for event in result.trace.events
            if event.type == "agent_message" and event.turn == 1
        ]
        self.assertTrue(followup_agent_messages)
        self.assertIn("summary", followup_agent_messages[-1].message or "")
        # No spurious tool_error event was appended.
        followup_tool_errors = [
            event for event in result.trace.events
            if event.type == "tool_error" and event.turn == 1
        ]
        self.assertEqual(followup_tool_errors, [])

    def test_followup_budget_is_FOLLOWUP_BUDGET(self) -> None:
        # Script one more call than FOLLOWUP_BUDGET allows; the (budget+1)th
        # call must trigger budget_exceeded. Reading the constant rather
        # than hard-coding "9" keeps the test honest if FOLLOWUP_BUDGET
        # moves again.
        initial = eval_agent_graph.analyze_run("run_1", llm_client=ScriptedLLM(_happy_script()))
        initial_events = [event.model_dump(mode="json") for event in initial.trace.events]
        script = [
            _tool_message("get_step_detail", {"run_id": "run_1", "step_index": 1, "image_detail": "high"})
            for _ in range(eval_agent_graph.FOLLOWUP_BUDGET + 1)
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
                    # Predictable seq: phase(preprocess)=0,
                    # tool_call(get_step_detail)=1, tool_result(get_step_detail)=2,
                    # tool_call(propose...)=3, tool_result(propose...)=4. Empty
                    # agent_message events are not recorded (scripted AIMessages
                    # carry only tool_calls), so the high-detail result is at
                    # seq 2 once the preprocess phase event is included.
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
