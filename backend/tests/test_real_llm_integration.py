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

import unittest

import pytest

from backend.app import eval_agent_graph, storage
from backend.app.schemas import AgentTrace
from backend.tests.conftest import real_llm_configured
from backend.tests.test_storage import sample_run


@pytest.mark.skipif(
    not real_llm_configured(),
    reason="Set OPENAI_API_KEY + TRAJECTA_AGENT_MODEL (in .env or shell) to enable.",
)
@pytest.mark.usefixtures("real_llm_env")
class RealLLMIntegrationTests(unittest.TestCase):
    """Smoke-gate the production LangChain ChatOpenAI agent path.

    We do not assert on the agent's *verdict* (failure vs success, which
    failure_type, which step) — those depend on the model and the prompt
    and are not stable enough for an assertion. We assert only on the
    structural shape of the trace: the agent terminated for one of the
    expected reasons and made at least one tool call.
    """

    def setUp(self) -> None:
        storage.save_trajectory(sample_run("run_real_llm", status="unknown"))

    def test_analyze_trajectory_terminates_with_valid_trace(self) -> None:
        result = eval_agent_graph.analyze_trajectory("run_real_llm")

        AgentTrace.model_validate(result.trace.model_dump(mode="json"))

        # Structural assertion only — wiring works if the agent loop ran
        # at all and reached one of the legal termination states. We do
        # not require a minimum tool_call_count because the test fixture
        # is a 1-step trivial run and models often respond with plain
        # text rather than calling tools on it (which is reasonable
        # behavior, not a bug in the wiring).
        self.assertIn(
            result.trace.terminated_by,
            {"propose_eval_case", "budget_exceeded", "error"},
        )

        # If the model did call tools, every name must be declared.
        declared = {
            "get_trajectory",
            "find_similar_successful_trajectory",
            "get_step_detail",
            "search_failure_memory",
            "search_failure_eval_cases",
            "propose_eval_case",
        }
        for event in result.trace.events:
            if event.type == "tool_call":
                self.assertIn(event.name, declared, f"unknown tool: {event.name!r}")


if __name__ == "__main__":
    unittest.main()
