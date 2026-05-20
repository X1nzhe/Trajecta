from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .eval_agent import EvalAgent
from .storage import LocalStorage

app = FastAPI(title="EvalTrace Lite API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
storage = LocalStorage()
agent = EvalAgent(storage=storage)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/runs")
def list_runs() -> dict[str, list[str]]:
    return {"runs": storage.list_runs()}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    try:
        return storage.load_run(run_id).model_dump()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@app.post("/analyze/{run_id}/{step_id}")
def analyze_step(run_id: str, step_id: str):
    try:
        return agent.analyze_step(run_id=run_id, step_id=step_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
