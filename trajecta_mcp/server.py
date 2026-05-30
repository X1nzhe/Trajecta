"""Trajecta MCP server — Phase 8 B1.

Exposes the Trajecta Eval Agent over MCP for external coding agents (Claude
Code, Cursor). The load-bearing tool is ``analyze_run``, which runs the
*entire* LangGraph Eval Agent loop as one composite call (B2). The other
five tools are read-only delegates to existing in-process backend functions.

Least privilege is enforced by tool surface: only the six functions below are
decorated with ``@mcp.tool``. Mutating / admin tools
(``save_validated_eval_case``, ``delete_*``, ``import_dataset``,
``set_prompt_version``) are deliberately not registered, so FastMCP returns
``method_not_found`` for them — there is no runtime permission check to
bypass. See docs/mcp.md and docs/security_governance.md.

Run it (from the repo root, in the ``trajecta`` env):

    python trajecta_mcp/server.py

The package is named ``trajecta_mcp`` (not ``mcp``) so a repo-root entry on
``sys.path`` never shadows the official ``mcp`` SDK that fastmcp imports.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

# When launched as ``python trajecta_mcp/server.py``, sys.path[0] is this
# directory, not the repo root — so ``from backend.app import …`` would fail.
# Insert the repo root explicitly. ``import mcp`` still resolves to the
# site-packages SDK because this package is ``trajecta_mcp``, not ``mcp``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastmcp import FastMCP  # noqa: E402

from backend.app import eval_agent_graph, storage, tools  # noqa: E402

mcp = FastMCP("Trajecta")


@mcp.tool
def list_runs() -> list[dict[str, Any]]:
    """List imported trajectory runs (metadata only)."""
    return [run.model_dump(mode="json") for run in storage.list_runs()]


@mcp.tool
def get_run(run_id: str) -> dict[str, Any]:
    """Fetch one run with its cached preprocessing digest attached."""
    return tools.get_run(run_id)


@mcp.tool
def get_step_detail(
    run_id: str,
    step_index: int,
    image_detail: Literal["low", "high"] = "high",
) -> dict[str, Any]:
    """Inspect one step in depth (raw detail, optional high-detail VLM call)."""
    return tools.get_step_detail(run_id, step_index, image_detail=image_detail)


@mcp.tool
def search_failure_memory(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Retrieve curated failure-pattern memory cases similar to the query."""
    return tools.search_failure_memory(query, top_k=top_k)


@mcp.tool
def search_eval_cases(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Retrieve prior human-validated EvalCase records similar to the query."""
    return tools.search_eval_cases(query, top_k=top_k, only_validated=True)


@mcp.tool
def analyze_run(run_id: str) -> dict[str, Any]:
    """Run the full LangGraph Eval Agent on a trajectory (composite).

    Spawns one Eval Agent run: preprocess → tool-calling loop (RAG retrieval,
    coarse-to-fine VLM via get_step_detail) → propose_eval_case. Returns an
    EvalCase **draft** (``human_validated=False`` — only Trajecta's own UI can
    flip that) plus the full AgentTrace with ``source="mcp"`` stamped. The
    per-turn tool-call budget applies exactly as on the HTTP path; a runaway
    loop ends with ``terminated_by="budget_exceeded"`` and the trace is still
    returned. There is no per-step mode — analysis is always full-trajectory.
    """
    result = eval_agent_graph.analyze_run(run_id, persist=True, source="mcp")
    # AgentTrace events are byte-sanitised at append time (no screenshot /
    # image bytes), so model_dump(mode="json") is already wire-safe.
    return {
        "eval_case_draft": result.eval_case_draft,
        "agent_trace": result.trace.model_dump(mode="json"),
    }


if __name__ == "__main__":
    mcp.run()
