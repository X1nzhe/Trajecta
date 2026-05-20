from __future__ import annotations

from fastapi import FastAPI

from backend.app.eval_agent import EvalAgent

app = FastAPI(title="Trajecta MCP")
agent = EvalAgent()


@app.get("/tools")
def list_tools() -> dict[str, list[str]]:
    return {"tools": ["analyze_step", "list_runs"]}


@app.post("/tools/analyze_step/{run_id}/{step_id}")
def analyze_step_tool(run_id: str, step_id: str):
    return agent.analyze_step(run_id, step_id)
