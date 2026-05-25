"""Opt-in integration test for the real LLM agent path.

Skipped by default. Activate by exporting ``OPENAI_API_KEY`` and
``TRAJECTA_AGENT_MODEL`` (e.g. ``gpt-4o-mini``) before running pytest::

    OPENAI_API_KEY=sk-... TRAJECTA_AGENT_MODEL=gpt-4o-mini pytest \\
        backend/tests/test_real_llm_integration.py -v

This test exercises the production codepath only — ``eval_agent_graph``
constructs ``ChatOpenAI(...).bind_tools(...)`` via ``_default_llm_client``
when both env vars are set. It is **not** part of the default CI suite
because each run costs real OpenAI tokens; this file is the smoke gate
for "is the real-LLM wiring still alive after a refactor".
"""

from __future__ import annotations

import os
import unittest

import pytest

from backend.app import eval_agent_graph, storage
from backend.app.schemas import AgentTrace
from backend.tests.test_storage import sample_run


_REAL_LLM_CONFIGURED = bool(
    os.environ.get("OPENAI_API_KEY") and os.environ.get("TRAJECTA_AGENT_MODEL")
)


@pytest.mark.skipif(
    not _REAL_LLM_CONFIGURED,
    reason="Set OPENAI_API_KEY + TRAJECTA_AGENT_MODEL to enable.",
)
class RealLLMIntegrationTests(unittest.TestCase):
    """Smoke-gate the production LangChain ChatOpenAI agent path.

    We do not assert on the agent's *verdict* (failure vs success, which
    failure_type, which step) — those depend on the model and the prompt
    and are not stable enough for an assertion. We assert only on the
    structural shape of the trace: the agent terminated for one of the
    expected reasons and made at least one tool call.
    """

    def setUp(self) -> None:
        storage.save_run(sample_run("run_real_llm", status="unknown"))

    def test_analyze_run_terminates_with_valid_trace(self) -> None:
        result = eval_agent_graph.analyze_run("run_real_llm")

        AgentTrace.model_validate(result.trace.model_dump(mode="json"))

        self.assertIn(
            result.trace.terminated_by,
            {"propose_eval_case", "budget_exceeded", "error"},
        )
        self.assertGreater(result.trace.tool_call_count, 0)

        tool_calls = [event for event in result.trace.events if event.type == "tool_call"]
        self.assertGreater(len(tool_calls), 0)

        # The system prompt is intentionally minimal in v1; we do not yet
        # assert the model chose `get_run` first. We only assert that every
        # tool name the model emitted is one of the declared tools.
        declared = {
            "get_run",
            "find_similar_successful_run",
            "get_step_detail",
            "search_failure_memory",
            "search_eval_cases",
            "propose_eval_case",
        }
        for event in tool_calls:
            self.assertIn(event.name, declared, f"unknown tool: {event.name!r}")


if __name__ == "__main__":
    unittest.main()
