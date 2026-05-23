"""FastAPI app for Phase 2 non-agent backend endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.app import dataset_importer, storage, tools
from backend.app.schemas import EvalCase


app = FastAPI(title="Trajecta API")


class ImportRequest(BaseModel):
    source_dir: str | None = None


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return [run.model_dump(mode="json") for run in storage.list_runs()]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    try:
        return tools.get_run(run_id)
    except FileNotFoundError as exc:
        raise _not_found("run not found") from exc


@app.get("/api/runs/{run_id}/digest")
def get_digest(run_id: str) -> dict:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    digest = storage.load_digest(run_id)
    if digest is None:
        raise _not_found("digest not found")
    return digest.model_dump(mode="json")


@app.get("/api/runs/{run_id}/steps/{step_index}")
def get_step(run_id: str, step_index: int) -> dict:
    try:
        run = storage.load_run(run_id)
    except FileNotFoundError as exc:
        raise _not_found("run not found") from exc

    for step in run.steps:
        if step.index == step_index:
            return step.model_dump(mode="json")
    raise _not_found("step not found")


@app.get("/api/runs/{run_id}/steps/{step_index}/detail")
def get_step_detail(run_id: str, step_index: int) -> None:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    raise HTTPException(status_code=501, detail="Phase 3 owns VLM step detail.")


@app.get("/api/runs/{run_id}/screenshots/{filename:path}")
def get_screenshot(run_id: str, filename: str) -> FileResponse:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    try:
        path = storage.screenshot_path(run_id, filename)
    except ValueError as exc:
        raise _not_found("screenshot not found") from exc
    if not path.exists() or not path.is_file():
        raise _not_found("screenshot not found")
    return FileResponse(path)


@app.post("/api/import/molmoweb-sample")
def import_molmoweb_sample(request: ImportRequest | None = None) -> dict:
    source_text = request.source_dir if request and request.source_dir else None
    if source_text and "://" in source_text:
        raise HTTPException(status_code=422, detail="source_dir must be a local path")
    if source_text:
        requested = Path(source_text).expanduser()
        source_dir = requested if requested.is_absolute() else storage.REPO_ROOT / requested
    else:
        source_dir = storage.raw_sample_dir() / "hf_dataset"
    source_dir = source_dir.resolve()
    if not source_dir.exists():
        raise _not_found(f"source dataset not found: {source_dir}")

    try:
        runs = dataset_importer.import_sample(source_dir)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for run in runs:
        storage.save_run(run)
        storage.delete_digest(run.run_id)
        assets = dataset_importer.get_imported_screenshot_assets(run.run_id)
        if assets:
            storage.save_screenshots(run.run_id, assets)

    return {"imported_count": len(runs), "runs": [run.model_dump(mode="json") for run in runs]}


@app.post("/api/eval-cases")
def create_eval_case(case: EvalCase) -> dict:
    if not case.human_validated:
        raise HTTPException(status_code=422, detail="POST /api/eval-cases requires human_validated=true")
    try:
        storage.save_eval_case(case)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return case.model_dump(mode="json")


@app.get("/api/eval-cases")
def list_eval_cases() -> list[dict]:
    return [case.model_dump(mode="json") for case in storage.load_eval_cases()]


@app.get("/api/failure-memory/search")
def search_failure_memory(q: str = Query(...), top_k: int = 3) -> list[dict]:
    return tools.search_failure_memory(q, top_k=top_k)


@app.get("/api/eval-cases/search")
def search_eval_cases(q: str = Query(...), top_k: int = 3, only_validated: bool = True) -> list[dict]:
    return tools.search_eval_cases(q, top_k=top_k, only_validated=only_validated)


@app.post("/api/runs/{run_id}/preprocess")
def preprocess_run(run_id: str) -> None:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    raise HTTPException(status_code=501, detail="Phase 3 owns trajectory preprocessing.")


@app.post("/api/runs/{run_id}/analyze")
def analyze_run(run_id: str) -> None:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    raise HTTPException(status_code=501, detail="Phase 3 owns the LangGraph Eval Agent.")


@app.post("/api/runs/{run_id}/steps/{step_index}/analyze")
def analyze_step(run_id: str, step_index: int) -> None:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    raise HTTPException(status_code=501, detail="Phase 3 owns the LangGraph Eval Agent.")


@app.post("/api/runs/{run_id}/followup")
def followup(run_id: str) -> None:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    raise HTTPException(status_code=501, detail="Phase 3 owns follow-up agent turns.")
