"""Phase 8 B1 + B2 — Trajecta MCP server surface + analyze_run invariants.

Skipped entirely when fastmcp is not importable (e.g. a base env without the
dep) so the rest of the suite stays green there. In the project ``trajecta``
env fastmcp is present and these run.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from backend.app import eval_agent_graph, rag, storage  # noqa: E402
from backend.app.schemas import FailureMemoryCase  # noqa: E402
from backend.tests.test_storage import sample_run  # noqa: E402
from trajecta_mcp import server  # noqa: E402


EXPECTED_TOOLS = {
    "list_runs",
    "get_run",
    "get_step_detail",
    "search_failure_memory",
    "search_eval_cases",
    "analyze_run",
}

# Mutating / admin surfaces that MUST NOT be reachable over MCP.
EXCLUDED_TOOLS = [
    "save_validated_eval_case",
    "delete_run",
    "delete_eval_case",
    "import_dataset",
    "set_prompt_version",
]


def _registered_tool_names() -> set[str]:
    tools = asyncio.run(server.mcp.list_tools())
    return {t.name for t in tools}


def _seed_failed_run() -> None:
    """Minimal data the OfflineAgentMock 5-stage script needs end-to-end."""
    rag._client_cache = None
    rag._embedding_cache = None
    storage.save_run(sample_run("run_1", status="failed"))
    storage.save_run(sample_run("success_run", status="success"))
    rag.upsert_successful_trajectory(sample_run("success_run", status="success"))
    rag.upsert_failure_memory(
        FailureMemoryCase(
            case_id="fm_missed_constraint_001",
            failure_type="missed_constraint",
            summary="The agent ignored a user constraint.",
            fix_hint="Re-check constraints before completion.",
            tags=["constraint"],
        )
    )


# --- B1: tool surface (least privilege) ---------------------------------


def test_exactly_six_tools_registered() -> None:
    assert _registered_tool_names() == EXPECTED_TOOLS


@pytest.mark.parametrize("name", EXCLUDED_TOOLS)
def test_excluded_tool_not_registered(name: str) -> None:
    assert name not in _registered_tool_names()


@pytest.mark.parametrize("name", EXCLUDED_TOOLS)
def test_excluded_tool_call_raises_method_not_found(name: str) -> None:
    async def _call() -> None:
        async with Client(server.mcp) as client:
            await client.call_tool(name, {})

    with pytest.raises(ToolError) as exc:
        asyncio.run(_call())
    assert "unknown tool" in str(exc.value).lower()


def test_list_runs_tool_returns_metadata() -> None:
    _seed_failed_run()
    runs = server.list_runs()
    ids = {r["run_id"] for r in runs}
    assert {"run_1", "success_run"} <= ids
    # Metadata only — the heavy/untrusted steps array must NOT leak.
    first = runs[0]
    assert "steps" not in first
    assert isinstance(first["step_count"], int)
    assert {"run_id", "task", "status", "step_count"} <= first.keys()


# --- B2: analyze_run composite invariants --------------------------------


def test_analyze_run_stamps_mcp_source_and_draft() -> None:
    _seed_failed_run()
    result = server.analyze_run("run_1")

    assert result["eval_case_draft"] is not None
    # HITL gate: the MCP surface can only ever return a draft.
    assert result["eval_case_draft"]["human_validated"] is False
    # Origin stamped for audit.
    assert result["agent_trace"]["source"] == "mcp"
    # No raw screenshot / image bytes on the wire.
    blob = json.dumps(result)
    for forbidden in ("screenshot_bytes", "image_bytes", "image_data"):
        assert forbidden not in blob


def test_analyze_run_trace_parity_with_http_path() -> None:
    _seed_failed_run()
    mcp_result = server.analyze_run("run_1")
    ui = eval_agent_graph.analyze_run("run_1", source="ui")

    mcp_trace = mcp_result["agent_trace"]
    # Differ only by source (+ timestamps); the substantive shape matches.
    assert mcp_trace["source"] == "mcp"
    assert ui.trace.source == "ui"
    assert mcp_trace["tool_call_count"] == ui.trace.tool_call_count
    assert mcp_trace["terminated_by"] == ui.trace.terminated_by
    assert (
        mcp_result["eval_case_draft"]["failure_type"]
        == ui.eval_case_draft["failure_type"]
    )


def test_analyze_run_budget_invariant_surfaces_terminated_by() -> None:
    _seed_failed_run()
    result = server.analyze_run("run_1")
    # The per-turn budget applies across the MCP boundary exactly as on the
    # HTTP path; terminated_by is always present (propose_eval_case here).
    assert result["agent_trace"]["terminated_by"] in {
        "propose_eval_case",
        "budget_exceeded",
    }
