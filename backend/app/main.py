"""FastAPI app for Trajecta backend endpoints."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env from the repo root before any module reads os.environ.
# Existing shell exports take precedence (override=False is the default)
# so `export OPENAI_API_KEY=...` still wins over the file.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from backend.app import db, dataset_importer, eval_agent_graph, preprocess, rag, storage, tools
from backend.app.schemas import EvalCase


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_schema()
    rag.hydrate_all()
    yield


app = FastAPI(title="Trajecta API", lifespan=_lifespan)


class ImportRequest(BaseModel):
    source_dir: str | None = None


class FollowupRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must be non-empty")
        return value


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
def get_step_detail(
    run_id: str,
    step_index: int,
    image_detail: Literal["low", "high"] = Query("high"),
) -> dict:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    result = tools.get_step_detail(run_id, step_index, image_detail=image_detail)
    tool_error = result.get("tool_error") if isinstance(result, dict) else None
    if tool_error:
        if "step_index" in tool_error and "not found" in tool_error:
            raise _not_found("step not found")
        raise HTTPException(status_code=422, detail=tool_error)
    return result


@app.get("/api/runs/{run_id}/screenshots/{filename:path}")
def get_screenshot(run_id: str, filename: str) -> Response:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    loaded = storage.load_screenshot_with_meta(run_id, filename)
    if loaded is None:
        raise _not_found("screenshot not found")
    data, media_type = loaded
    return Response(content=data, media_type=media_type)


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
        # Re-import drop rule: a run that previously had a human-validated
        # eval case (and therefore a successful_runs row) loses that row on
        # re-import because the trajectory is now considered re-imported and
        # unanalyzed. The successful_runs collection only fills again when
        # the user re-validates the new analysis.
        rag.delete_successful_run(run.run_id)

    return {"imported_count": len(runs), "runs": [run.model_dump(mode="json") for run in runs]}


@app.post("/api/eval-cases")
def create_eval_case(case: EvalCase) -> dict:
    if not case.human_validated:
        raise HTTPException(status_code=422, detail="POST /api/eval-cases requires human_validated=true")
    try:
        run = storage.load_run(case.source_run_id)
    except FileNotFoundError as exc:
        raise _not_found(f"source run not found: {case.source_run_id}") from exc

    try:
        storage.save_eval_case(case)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Flip run.status on validation. Success cases are also indexed into
    # successful_runs so find_similar_successful_run can use them; failure
    # cases land in the eval_cases collection (existing behavior).
    new_status = "success" if case.is_success else "failed"
    updated_run = run.model_copy(update={"status": new_status})
    storage.save_run(updated_run)

    if case.is_success:
        rag.upsert_successful_run(updated_run)
    else:
        # A run can only sit in one of the two RAG collections at a time.
        # If a previous success-shape case had indexed this run into
        # successful_runs, evict it now so find_similar_successful_run
        # does not keep returning a now-failed comparator. Idempotent.
        rag.delete_successful_run(case.source_run_id)
        rag.upsert_eval_case(case)
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
def preprocess_run(run_id: str) -> dict:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    try:
        digest = preprocess.load_or_build_digest(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return digest.model_dump(mode="json")


@app.post("/api/runs/{run_id}/analyze")
def analyze_run(run_id: str) -> StreamingResponse:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    return _stream_agent_result(lambda: eval_agent_graph.stream_analyze_run(run_id))


@app.post("/api/runs/{run_id}/steps/{step_index}/analyze")
def analyze_step(run_id: str, step_index: int) -> StreamingResponse:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    return _stream_agent_result(lambda: eval_agent_graph.stream_analyze_step(run_id, step_index))


@app.post("/api/runs/{run_id}/followup")
def followup(run_id: str, request: FollowupRequest) -> StreamingResponse:
    if not storage.run_exists(run_id):
        raise _not_found("run not found")
    if storage.load_trace(run_id) is None:
        raise HTTPException(status_code=409, detail="follow-up requires an existing analysis trace")
    return _stream_agent_result(lambda: eval_agent_graph.stream_followup(run_id, request.message))


def _stream_agent_result(factory) -> StreamingResponse:
    def iter_lines() -> Iterator[str]:
        try:
            for item in factory():
                if isinstance(item, eval_agent_graph.AgentStreamDone):
                    result = item.result
                    yield json.dumps(
                        {
                            "type": "done",
                            "eval_case_draft": result.eval_case_draft,
                            "agent_trace": result.trace.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ) + "\n"
                    return

                yield json.dumps(
                    {"type": "event", "event": item.model_dump(mode="json")},
                    ensure_ascii=False,
                ) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "error": str(exc)}, ensure_ascii=False) + "\n"
            return
        yield json.dumps({"type": "error", "error": "agent stream ended without a terminal result"}) + "\n"

    return StreamingResponse(iter_lines(), media_type="application/x-ndjson")
