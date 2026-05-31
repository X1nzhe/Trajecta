# MCP — Trajecta's Composite Eval-Agent Tool

This document is the design source of truth for `trajecta_mcp/server.py` (shipped in Phase 8 B1).
Phase 8 B1–B3 were lower priority than the Gemini judge agreement path;
PROJECT.md and README.md link here for details. The B1.5 live-client smoke
is complete: the operator verified the server with MCP Inspector.

## Position in the Ecosystem

The browser-agent ecosystem already covers two layers:

- **Browser control** — browser-use / Browserbase / Playwright MCP
  servers drive a browser end-to-end.
- **Trajectory datasets** — MolmoWeb-HumanSkills, WebArena, Agent-Eval-Refine
  publish recorded browser-agent trajectories.

What is **missing** is a remote callable agent that takes a recorded
trajectory and produces a structured failure analysis with retrieval-grounded
evidence and a regression-eval-case draft. The Trajecta MCP composite
is that layer.

Trajecta MCP does **not** control browsers. It analyses trajectories
produced by other agents.

## Tool Surface

| Tool | Backend delegate | Side effects |
| --- | --- | --- |
| `list_runs` | `storage.list_runs` | None (read-only). |
| `get_run` | `storage.load_run` + cached digest | None. |
| `get_step_detail` | existing in-process tool | One high-detail VLM call; cost-bearing; logged in `AgentTrace`. |
| `search_failure_memory` | `rag.search_failure_memory` | None (read-only ChromaDB query). |
| `search_eval_cases` | `rag.search_eval_cases` | None; defaults to `human_validated=true`. |
| `analyze_run` | `eval_agent_graph.analyze_run` (composite) | Spawns one full Eval Agent run; cost-bearing; trace persisted to SQLite `traces` table with `source="mcp"`. |

### Tools intentionally **not** exposed

| Tool | Reason |
| --- | --- |
| `save_validated_eval_case` | HITL gate. `EvalCase.human_validated` can only be flipped through Trajecta's own UI. An external agent must not be able to certify cases. |
| `delete_run`, `delete_eval_case`, any destructive op | No remote mutation of historical data. |
| `import_dataset` | Admin-level surface; outside the analysis scope. |
| `set_prompt_version` | Prompt selection belongs to operator-controlled env vars, not external agents. |

The exclusion list is enforced by tool surface — `trajecta_mcp/server.py` does
not register these names — not by post-hoc permission checks. Phase 8
B4 (`docs/security_governance.md`) cites this surface as the primary
least-privilege mechanism.

## `analyze_run` as a Composite Tool

`analyze_run` is the load-bearing tool in this MCP server. It does not
forward to a single backend function; it exposes the entire LangGraph
Eval Agent loop as one MCP call.

```text
MCP client (Claude Code, Cursor)
    │ call analyze_run(run_id)
    ▼
trajecta_mcp/server.py
    │ delegates in-process
    ▼
eval_agent_graph.analyze_run(run_id, persist=True, source="mcp")
    │ preprocess (low-detail VLM per step, fixed for-loop)
    │ agent loop:
    │   reason → get_step_detail / search_failure_memory /
    │            search_eval_cases / find_similar_successful_run → reason
    │ propose_eval_case (terminal)
    ▼
EvalCase draft (human_validated=False) + AgentTrace
    │ serialised; large binary fields stripped
    ▼
MCP client receives one JSON payload
```

### Invariants

- **Budget honoured across the MCP boundary.** The per-turn tool-call
  budget (default 8) applies inside the MCP call exactly as inside the
  HTTP analyze path. Runaway loops produce `terminated_by="budget_exceeded"`
  and the trace is still returned — the client sees the partial work.
- **`source="mcp"` stamped on the trace.** `AgentTrace.source` records
  whether the run was initiated from the UI (`"ui"`), the eval harness
  (`"eval"`), or the MCP server (`"mcp"`). Audit code in
  `docs/security_governance.md` Mechanism 7 keys off this field.
- **`human_validated=false`.** The returned `EvalCase` is a draft. There
  is no MCP-side path to set it true; that requires the Trajecta UI.
- **Trace shape parity.** A trace produced via MCP equals a trace
  produced via `POST /api/runs/{id}/analyze` modulo the `source` field
  and timestamps. The same `tool_call_count`, `terminated_by`,
  `eval_case_draft`, and event sequence are present.

### Why expose the whole agent rather than individual tools

A naive MCP design would expose Trajecta's six internal tools and let
the external coding agent orchestrate them. We don't do that for three
reasons:

1. **Budget enforcement.** A coordinated tool sequence driven by an
   external agent cannot share the per-turn budget that bounds Eval
   Agent cost. Splitting the loop across the MCP boundary breaks the
   budget contract.
2. **Trace integrity.** Internal tool calls produce one cohesive
   `AgentTrace` with monotonic `seq` and `turn`. Splitting orchestration
   across two agents would produce two disjoint traces with no shared
   reasoning chain — neither RAGAS nor Phase 8's judge can score that.
3. **Component composition.** A single `analyze_run` MCP call exercises
   RAG (failure memory + eval cases retrieval), Tools (the six in-process
   tools), Security/Governance (budget + audit + HITL gate), and the
   Eval Agent itself simultaneously. That composition is the artefact
   we are presenting.

## Client Configuration

Add to `claude_desktop_config.json` (or the Cursor
equivalent):

```json
{
  "mcpServers": {
    "trajecta": {
      "command": "python",
      "args": ["trajecta_mcp/server.py"],
      "cwd": "<path to Trajecta repo>"
    }
  }
}
```

We pass `trajecta_mcp/server.py` as a script path rather than `-m trajecta_mcp.server` to
avoid any chance of Python resolving `mcp` against the installed official
SDK package. The script form is unambiguous.

Alternative: `fastmcp run trajecta_mcp/server.py:mcp` if FastMCP CLI is installed
globally. Both produce identical stdio-transport behaviour.

Restart the client. `list_runs`, `analyze_run`, and the four other tools
should appear under the `trajecta` namespace.

## MCP Inspector Smoke Test

The Phase 8 B1.5 live-client smoke was verified by the operator with MCP
Inspector. To repeat it from the repo root, use an environment where
`backend/requirements.txt` has already been installed:

```bash
npx @modelcontextprotocol/inspector python trajecta_mcp/server.py
```

Manual acceptance criteria:

1. The Inspector connects to the stdio server.
2. The Tools tab lists exactly six tools: `list_runs`, `get_run`,
   `get_step_detail`, `search_failure_memory`, `search_eval_cases`,
   `analyze_run`.
3. `list_runs` returns imported Trajecta runs.
4. `analyze_run` returns an `eval_case_draft` and an `agent_trace`.
5. The returned trace has `agent_trace.source == "mcp"`.
6. Mutation and admin tools are absent: `save_validated_eval_case`,
   `delete_*`, `import_dataset`, and `set_prompt_version`.

This smoke proves client connectivity and tool execution. It does not
change the HITL boundary: validation and export still happen through the
Trajecta UI.

## Demo Script

The seven-step demo lives canonically here; `README.md` § "MCP
Connection" mirrors the user-facing version.

1. Operator pre-imports MolmoWeb sample runs into Trajecta storage
   (one-time setup).
2. In Claude Code, user asks: *"List my Trajecta runs."*
3. Claude Code calls `trajecta.list_runs()`. It picks a failed sample.
4. User asks: *"Why did this booking run fail?"*
5. Claude Code calls `trajecta.analyze_run(run_id)`.
6. Trajecta runs the Eval Agent end-to-end:
   - Preprocess builds the trajectory digest.
   - Agent loop deep-inspects suspicious steps via `get_step_detail`.
   - Agent calls `search_failure_memory` to retrieve similar precedents.
   - Agent terminates via `propose_eval_case`.
7. Claude Code receives the `EvalCase` draft + `AgentTrace` and
   summarises for the user.

Validation is **not** part of the MCP demo. Users open the Trajecta UI to
review, edit, and mark a draft `human_validated=true`. This is by design.

## Implementation Notes

### Framework: FastMCP (standalone)

We use the **standalone `fastmcp` package** (`pip install fastmcp`) rather
than the FastMCP class shipped inside the official `mcp[cli]` SDK. The
server package is named **`trajecta_mcp/`** (not `mcp/`): the server adds
the repo root to `sys.path` for `from backend.app import …`, and a
top-level `mcp/` package on that path would shadow the official `mcp` SDK
that `fastmcp` imports internally. Naming it `trajecta_mcp` keeps
`import mcp` resolving to the installed SDK.

### Server skeleton

```python
# trajecta_mcp/server.py
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastmcp import FastMCP

from backend.app import eval_agent_graph, storage, tools

mcp = FastMCP("Trajecta")

@mcp.tool
def list_runs() -> list[dict]:
    """List imported trajectory runs (metadata only — no steps array)."""
    return [
        {"run_id": r.run_id, "task": r.task, "source": r.source,
         "status": r.status, "step_count": len(r.steps), "metadata": r.metadata}
        for r in storage.list_runs()
    ]

@mcp.tool
def analyze_run(run_id: str) -> dict:
    """Run the full LangGraph Eval Agent on a trajectory (full-run only).

    Returns an EvalCase draft (human_validated=False) plus the full
    AgentTrace with source="mcp" stamped. There is no per-step mode — the
    backend analyze_run analyses the whole trajectory.
    """
    result = eval_agent_graph.analyze_run(run_id, persist=True, source="mcp")
    return {
        "eval_case_draft": result.eval_case_draft,
        # AgentTrace events are byte-sanitised at append time, so the dump
        # carries no screenshot/image bytes.
        "agent_trace": result.trace.model_dump(mode="json"),
    }

# (Four more @mcp.tool delegates for get_run, get_step_detail,
#  search_failure_memory, search_eval_cases.)

if __name__ == "__main__":
    mcp.run()
```

### What FastMCP gives us for free

- **JSON-Schema input validation** is auto-derived from Python type hints
  on each tool function. We do not write `inputSchema` blocks by hand.
- **Tool discovery** is decorator-based — tools registered with
  `@mcp.tool()` are exposed; tools not decorated are unreachable.
  Excluded tools (`save_validated_eval_case`, etc.) simply lack the
  decorator; there is no runtime permission check that could be
  bypassed.
- **`method_not_found`** responses are emitted automatically when a
  client invokes an unregistered tool name.
- **stdio transport** is the default — `mcp.run()` speaks MCP over
  stdin/stdout, which is what Claude Code / Cursor expect.

### What we still write by hand

- **Backend delegation** — each tool function calls the existing
  in-process backend function (`storage.list_runs`,
  `eval_agent_graph.analyze_run`, etc.). No logic is duplicated.
- **Payload sanitisation** — `analyze_run` strips screenshot bytes from
  the returned trace (FastMCP serialises the dict as JSON; we don't want
  base64-encoded PNGs on the wire).
- **Trace stamping** — `analyze_run` passes `source="mcp"` through to
  `eval_agent_graph.analyze_run` so the persisted trace records the
  MCP origin for audit (see
  [docs/security_governance.md](security_governance.md) § Mechanism 5).

### Process model

One process. The MCP server imports `eval_agent_graph` and `storage`
in-process; there is no separate analysis worker. Tool calls and the
agent loop share one Python interpreter and one SQLite connection.

### Dependency

Add `fastmcp>=2.0` to `backend/requirements.txt` (Phase 8 B1 code work,
not part of this design doc).

## Boundary with Browser-Control MCP Servers

| Concern | browser-control MCP (browser-use, Browserbase) | Trajecta MCP |
| --- | --- | --- |
| Browser control | Yes | No |
| Trajectory analysis | No | Yes |
| RAG over failure memory | No | Yes |
| Produces EvalCase drafts | No | Yes |
| Persists validated cases | N/A | Only via Trajecta UI; not via MCP |
| Suitable for online agents | Yes | No (offline analysis) |

The two layers are complementary. A coding agent using both can drive a
browser via browser-use MCP, record the trajectory, and then call
Trajecta MCP `analyze_run` to diagnose any failure.
