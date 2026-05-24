# Contracts

This file is the single source of truth for shared Trajecta contracts:

- Pydantic schemas
- agent tool contracts
- FastAPI endpoint surface
- ChromaDB collection contracts
- screenshot access rules

Topic docs may explain behavior and implementation strategy, but they should not
redefine fields, endpoint lists, or tool signatures.

## v1 Assumptions

- **Single user, single concurrency.** v1 assumes one analyze request at a time per run; concurrent analyzes on the same `run_id` race on `last_trace.json` and have undefined behavior.
- **No force-rebuild knob.** `POST /api/runs/{run_id}/preprocess` is cache-first with no override; rebuilding a digest requires deleting `digest.json` on disk or bumping `preprocess_version` in code.
- **Missing screenshots are non-fatal.** If `StepObservation.screenshot` references a row that is absent from the `screenshots` table, `has_screenshot=false` is recorded in the digest, `get_step_detail` returns no VLM summary, and `GET /api/runs/{run_id}/screenshots/{filename}` returns `404`.

## Schema Contracts

Create `backend/app/schemas.py` from these Pydantic models.

```python
from pydantic import BaseModel, Field
from typing import Literal, Optional, List, Dict, Any


class Coordinate(BaseModel):
    x: float
    y: float


class BBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class StepAction(BaseModel):
    type: Literal["click", "type", "scroll", "navigate", "wait", "unknown"]
    label: Optional[str] = None
    text: Optional[str] = None
    coordinates: Optional[Coordinate] = None
    bbox: Optional[BBox] = None
    raw: Optional[str] = None


class StepObservation(BaseModel):
    screenshot: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    visible_text: Optional[str] = None
    visual_evidence: List[str] = Field(default_factory=list)


class StepResult(BaseModel):
    status: Literal["success", "failed", "unknown"] = "unknown"
    error: Optional[str] = None


class CoordinateValidation(BaseModel):
    status: Literal["validated", "out_of_bounds", "missing", "unknown"] = "unknown"
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    reason: Optional[str] = None


class TrajectoryStep(BaseModel):
    index: int
    timestamp: Optional[str] = None
    observation: StepObservation
    action: StepAction
    result: StepResult = Field(default_factory=StepResult)
    coordinate_validation: CoordinateValidation = Field(default_factory=CoordinateValidation)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TrajectoryRun(BaseModel):
    run_id: str
    task: str
    source: str = "allenai/MolmoWeb-HumanSkills"
    status: Literal["success", "failed", "unknown"] = "unknown"
    steps: List[TrajectoryStep]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class StepDigest(BaseModel):
    index: int
    action_type: Literal["click", "type", "scroll", "navigate", "wait", "unknown"]
    action_text: str
    action_target: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    result_status: Literal["success", "failed", "unknown"] = "unknown"
    coord_validation_status: Literal["validated", "out_of_bounds", "missing", "unknown"] = "unknown"
    vlm_low_detail_summary: Optional[str] = None
    has_screenshot: bool = False


class TrajectoryDigest(BaseModel):
    run_id: str
    task: str
    step_count: int
    steps: List[StepDigest]
    preprocess_model: Optional[str] = None
    preprocess_version: str = "v1"


class FailureMemoryCase(BaseModel):
    case_id: str
    failure_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    summary: str
    fix_hint: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_run_id: Optional[str] = None


class EvidenceItem(BaseModel):
    claim: str
    source: Literal[
        "trajectory",
        "trajectory_digest",
        "step_detail_high",
        "step_detail_low",
        "failure_memory",
        "eval_case",
        "successful_run",
        "unavailable",
    ]
    run_id: Optional[str] = None
    step_index: Optional[int] = None
    trace_event_seq: Optional[int] = None
    context_id: Optional[str] = None


class EvalCase(BaseModel):
    case_id: str
    source_run_id: str
    task: str
    failure_step: int
    failure_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expected_behavior: str
    actual_behavior: str
    evidence: List[EvidenceItem]
    regression_rule: str
    retrieved_context_ids: List[str] = Field(default_factory=list)
    human_validated: bool = False


class AgentTraceEvent(BaseModel):
    seq: int
    type: Literal["agent_message", "user_message", "tool_call", "tool_result", "tool_error"]
    name: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    error: Optional[str] = None
    turn: int = 0


class AgentTrace(BaseModel):
    run_id: str
    user_intent: Literal["analyze_run", "analyze_step"]
    selected_step: Optional[int] = None
    tool_call_count: int = 0
    turn_count: int = 1
    terminated_by: Literal["propose_eval_case", "budget_exceeded", "error"] = "error"
    events: List[AgentTraceEvent] = Field(default_factory=list)
```

Schema field notes:

- `AgentTraceEvent.seq` starts at `0` and increments by `1` within one trace, across all turns.
- `AgentTraceEvent.turn` starts at `0` for the initial `analyze` invocation and increments by `1` for each follow-up turn. Events recorded inside the same turn share the same `turn` value.
- `AgentTraceEvent.type == "user_message"` records the follow-up message text in `message`. The initial `analyze` invocation is implicit and does not produce a `user_message` event.
- `AgentTrace.turn_count` equals the number of distinct turns recorded so far; it starts at `1` after the initial analyze and increments by `1` per follow-up.
- `AgentTrace.tool_call_count` is **cumulative across all turns**. Per-turn budget enforcement is the agent loop's responsibility (see [docs/eval_agent.md](eval_agent.md)); the trace persists only the running total for observability.
- `AgentTrace.terminated_by` reflects the **latest turn's** termination reason. Earlier-turn outcomes are recoverable by walking events backward to find each turn's last event.
- `TrajectoryRun.status` is imported from the source trajectory when present. If source data does not provide a reliable run-level result, set it to `"unknown"`; do not infer it from the Eval Agent's analysis.
- `StepObservation.visual_evidence` is for structured visual evidence imported from the source dataset or explicit high-detail inspection output. It must not be populated from low-detail preprocessing hints.
- Low-detail preprocessing output belongs in `StepDigest.vlm_low_detail_summary`, not in `StepObservation`.
- `StepAction.bbox` is untrusted unless validated against screenshot dimensions. v1 may omit bbox overlays; if rendered, the bbox must be in bounds and tied to a valid screenshot.
- `EvalCase.evidence` is a list of structured `EvidenceItem` objects, not free-form strings. The frontend renders `claim`, but the rest of the object is used to jump back to the supporting step, tool event, or retrieved context.
- `EvidenceItem.trace_event_seq` points to the `AgentTraceEvent.seq` that produced the evidence when it came from a tool call or tool result. It is `None` for static trajectory fields or unavailable evidence.
- `EvidenceItem.context_id` stores a `FailureMemoryCase.case_id` or `EvalCase.case_id` when `source` is `"failure_memory"` or `"eval_case"`. It must appear in `retrieved_context_ids` if the final eval case relies on it.
- `source="step_detail_low"` may be used for orientation only. It must not be the sole support for final claims about visual text, target identity, or coordinate correctness.
- `source="unavailable"` records absence of evidence, such as a missing screenshot, invalid coordinate, or unavailable successful comparison run. The `claim` must state what was unavailable.

## ID Conventions

- `failure_type` must match `^[a-z][a-z0-9_]*$`.
- Failure memory IDs: `fm_{failure_type}_{NNN}`, for example `fm_missed_constraint_001`.
- Eval case IDs: `ec_{source_run_id}_step_{failure_step}`. If collisions are possible, append `_{failure_type}`.
- `retrieved_context_ids` must contain IDs returned by `search_failure_memory` or `search_eval_cases` in the same agent trace.

ID generators:

- Failure memory IDs are manually assigned in `data/failure_memory/cases.jsonl`; import code must reject duplicates and IDs that do not match `^fm_[a-z][a-z0-9_]*_[0-9]{3}$`. `NNN` is monotonically assigned within each `failure_type`.
- Eval case IDs are generated by backend code before returning from `propose_eval_case`. Use `make_eval_case_id(run_id, failure_step, failure_type, storage)` in `backend/app/ids.py`; it first tries `ec_{run_id}_step_{failure_step}` and appends `_{failure_type}` only if the base ID already exists.

## Screenshot Contract

`StepObservation.screenshot` stores a plain filename (no directory component).
The bytes live as a BLOB row in the SQLite `screenshots` table keyed by
`(run_id, filename)`; storage is **not** filesystem-backed. The filename must
not contain path separators or absolute paths.

API responses surface a derived frontend URL:

```text
/api/runs/{run_id}/screenshots/{filename}
```

Screenshot endpoint rules:

- Stream the BLOB bytes from SQLite with the correct media type.
- Return `404` if the run or screenshot row does not exist.
- Reject path traversal in the filename component (`storage._safe_id` enforces `^[A-Za-z0-9_.-]{1,256}$`).
- Do not expose raw absolute filesystem paths to the frontend.

## Storage Contract

Persistence is owned by `backend/app/storage.py`. Backed by a single SQLite
database (`data/trajecta.db`) accessed through SQLAlchemy 2.0; all other
modules must reach the DB through `storage.*` — no direct queries elsewhere.

Layout:

```text
data/
  raw/molmoweb_humanskills_sample/
    run_status_overlay.json          # hand-curated status, see docs/dataset_import.md
  trajecta.db                        # SQLite: runs, steps, screenshots,
                                     # digests, traces, eval_cases, failure_memory
  failure_memory/cases.jsonl         # FailureMemoryCase seed corpus (hydrated into DB on load)
  chroma/                            # ChromaDB persistence (TRAJECTA_CHROMA_DIR override)
```

Schema is defined as SQLAlchemy declarative models in `backend/app/models.py`
and tracked by Alembic in `backend/alembic/versions/`. The app calls
`Base.metadata.create_all` on startup so a fresh checkout boots without
running `alembic upgrade head` manually; production deployments should still
prefer `alembic upgrade head` for migration safety.

Function surface:

```python
# Runs
def load_run(run_id: str) -> TrajectoryRun: ...
def save_run(run: TrajectoryRun) -> None: ...
def list_runs() -> list[TrajectoryRun]: ...
def run_exists(run_id: str) -> bool: ...

# Digest
def load_digest(run_id: str) -> Optional[TrajectoryDigest]: ...
def save_digest(run_id: str, digest: TrajectoryDigest) -> None: ...

# Trace
def load_trace(run_id: str) -> Optional[AgentTrace]: ...
def save_trace(run_id: str, trace: AgentTrace) -> None: ...

# Eval cases
def save_eval_case(case: EvalCase) -> None: ...
def load_eval_case(case_id: str) -> Optional[EvalCase]: ...
def load_eval_cases() -> list[EvalCase]: ...
def eval_case_exists(case_id: str) -> bool: ...

# Failure memory
def load_failure_memory() -> list[FailureMemoryCase]: ...
```

Behavior rules:

- `load_run` and `load_digest` raise `FileNotFoundError` for unknown run IDs; API layer converts to `404`.
- `load_trace`, `load_digest`, `load_eval_case` return `None` when the artifact does not exist (it is normal for a run to have no trace yet).
- `save_run`, `save_trace`, `save_digest`, `save_eval_case` write inside a transaction (`session_scope`); commit on clean exit, rollback on exception. `save_run` replaces the existing row + cascades step deletes, so re-imports cannot leave stale step rows.
- `list_runs` executes one `SELECT * FROM runs ORDER BY run_id` plus per-row step joins (relationship loaded eagerly). Demo fixtures are small (≤ ~50 runs) so the simple read pattern is fine.
- `load_failure_memory` reads `data/failure_memory/cases.jsonl` as source of truth, validates every row, raises on duplicate `case_id`, then refreshes the `failure_memory` DB table from the file on each call. The JSONL stays editable by hand; the DB is just a queryable mirror.
- `save_eval_case` inserts into the `eval_cases` table and refuses duplicate `case_id` (raises; the API layer surfaces this as 409). It must also call into `rag.upsert_eval_case(case)` so ChromaDB stays in sync; see "Index trigger" rules below.
- Eval-case drafts (`human_validated=false`) are **not** persisted in v1. The draft survives only in the API response and in the trace's `propose_eval_case` tool-call args. Refreshing the page = lose the draft = re-analyze.
- `run_exists` is used by `ids.make_eval_case_id` for collision checks; it must be cheap (a single primary-key lookup against `runs`).
- `load_screenshot(run_id, filename) -> bytes | None` is the only way to read screenshot bytes (there is no path-on-disk anymore). VLM callers pass the bytes directly to `llm.summarize_*`.



Implement these typed tools in `backend/app/tools.py`.

```python
def get_run(run_id: str) -> dict:
    """Return trajectory run metadata and the cached or freshly built digest.

    Accepts any imported `run_id`, not just the run currently under analysis.
    The agent uses this to load comparison runs returned by
    `find_similar_successful_run`.
    """


def find_similar_successful_run(
    task: str,
    top_k: int = 3,
    exclude_run_id: str | None = None,
) -> list[dict]:
    """Retrieve previously imported runs whose task is semantically similar to
    `task` AND whose `TrajectoryRun.status == "success"`.

    Used by the agent for replay-and-diff: after identifying a likely failure
    step in the current run, the agent calls this to find a comparable
    successful run, then calls `get_run(other_run_id)` to load that run's
    digest and reasons about where the two runs diverge.

    Returns a list of dicts with:
    - `run_id`: str
    - `task`: str
    - `status`: Literal["success"]   (filtered)
    - `step_count`: int

    `exclude_run_id` is used for the currently analyzed run, when known, so it
    is excluded from results. The list is sorted by similarity, highest first.
    May return an empty list.

    Run IDs returned here are **not** part of `EvalCase.retrieved_context_ids`
    — that field stores failure-memory and eval-case IDs only. The comparison
    is traceable through the agent's `AgentTrace`.
    """


def get_step_detail(
    run_id: str,
    step_index: int,
    image_detail: Literal["low", "high"] = "high",
) -> dict:
    """Return VLM analysis for one step, without screenshot bytes.

    `image_detail` selects the VLM resolution:
    - "high" (default): ~1500 tokens/image; required for any claim about
      visual text, target identity, or coordinate correctness.
    - "low": ~85 tokens/image; allowed for orientation and suspicious-step
      selection only. The agent must not cite low-detail output as final
      evidence — see Screenshot Detail Policy in docs/eval_agent.md.
    """


def search_failure_memory(query: str, top_k: int = 3) -> list[dict]:
    """Retrieve FailureMemoryCase-like records from the failure_memory collection."""


def search_eval_cases(query: str, top_k: int = 3, only_validated: bool = True) -> list[dict]:
    """Retrieve EvalCase-like records from the eval_cases collection."""


def propose_eval_case(
    run_id: str,
    failure_step: int,
    failure_type: str,
    expected_behavior: str,
    actual_behavior: str,
    evidence: list[EvidenceItem],
    regression_rule: str,
    retrieved_context_ids: list[str],
) -> dict:
    """Terminal tool that returns an EvalCase draft with human_validated=false."""
```

`propose_eval_case` ends the agent loop. The returned draft must validate as
`EvalCase`, including `EvidenceItem` objects for every evidence entry, and
`human_validated` must remain `false` until user review.

Implementation responsibilities:

- `get_run` is an agent tool. The agent should call it at the start of `agent_loop` to load run metadata and the digest.
- `propose_eval_case` loads `TrajectoryRun.task` by `run_id` and injects it as `EvalCase.task`.
- `propose_eval_case` computes `EvalCase.case_id` through `make_eval_case_id(...)`.
- `propose_eval_case` copies `run_id` into `EvalCase.source_run_id`.
- `propose_eval_case` sets `human_validated=False`.
- `propose_eval_case` validates that every `retrieved_context_id` appears in a prior `search_failure_memory` or `search_eval_cases` tool result in the current `AgentTrace`.
- The tool-call budget counts `get_step_detail`, `search_failure_memory`, `search_eval_cases`, and `find_similar_successful_run`. `get_run` and `propose_eval_case` do not count against the budget.

## API Contracts

```text
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/digest
GET  /api/runs/{run_id}/steps/{step_index}
GET  /api/runs/{run_id}/steps/{step_index}/detail
GET  /api/runs/{run_id}/screenshots/{filename}

POST /api/import/molmoweb-sample
POST /api/runs/{run_id}/preprocess
POST /api/runs/{run_id}/analyze
POST /api/runs/{run_id}/steps/{step_index}/analyze
POST /api/runs/{run_id}/followup

GET  /api/failure-memory/search?q=...
GET  /api/eval-cases/search?q=...
POST /api/eval-cases
GET  /api/eval-cases
```

`POST /api/runs/{run_id}/analyze`, `POST /api/runs/{run_id}/steps/{step_index}/analyze`, and `POST /api/runs/{run_id}/followup` all return an **NDJSON stream** (`Content-Type: application/x-ndjson`). Each line is one JSON object terminated by `\n`. There are three line types:

```jsonc
// 0..N event lines, one per new AgentTraceEvent as it is appended
{"type": "event", "event": { /* AgentTraceEvent */ }}

// exactly one terminal line — the canonical final state
{"type": "done",  "eval_case_draft": { /* EvalCase */ } | null,
                  "agent_trace":    { /* AgentTrace */ }}

// alternative terminal line for unrecoverable errors (network/agent crash,
// not the agent's tool_error events — those still stream as `event` lines)
{"type": "error", "error": "string"}
```

Stream rules:

- `event` lines carry **only new events being appended in this request**. For `/analyze` (fresh trace) the stream starts at `seq=0`; for `/followup` it starts at `prior_max_seq + 1`. The frontend appends to its local trace in arrival order.
- Exactly one `done` **or** one `error` line ends the stream; the server closes the connection after writing it. After `done`, no more lines are produced.
- `done.agent_trace` contains the **complete** trace (including events from previous turns if any) — this is the canonical state for the frontend to reconcile against.
- `done.eval_case_draft` is `null` whenever the latest turn terminated by `budget_exceeded` or `error`. `tool_call_count`, `turn_count`, and `terminated_by` are nested under `agent_trace`; they are not duplicated as top-level response fields.
- Order invariant: every `event` line precedes the terminal `done`/`error`. Streamed `event.seq` values are strictly increasing.
- Clients that don't want incremental updates may ignore `event` lines and only consume `done` / `error`. Both behaviors are supported.

Endpoint-to-agent mapping:

- `POST /api/runs/{run_id}/analyze` sets `user_intent="analyze_run"` and `selected_step=None`. Creates a fresh trace at turn 0.
- `POST /api/runs/{run_id}/steps/{step_index}/analyze` sets `user_intent="analyze_step"` and `selected_step=step_index`. Creates a fresh trace at turn 0.
- `POST /api/runs/{run_id}/followup` continues the existing `last_trace.json`. See the follow-up contract below.

### Follow-up Contract

`POST /api/runs/{run_id}/followup` is the second-and-onward turn of an
already-started analysis. Request body:

```jsonc
{
  "message": "string (user's follow-up question, non-empty, <= 2000 chars)"
}
```

Response is the **same NDJSON stream** as `/analyze` (`Content-Type: application/x-ndjson`, three line types: `event`, `done`, `error`). `event` lines for follow-up start at `prior_max_seq + 1` and only carry events appended in this turn; the terminal `done` carries the complete updated trace.

Preconditions:

- `404` if `run_id` is unknown.
- `409` if no `last_trace.json` exists for the run (the user must call `/analyze` first).
- `422` if `message` is missing, empty, or exceeds 2000 characters.

Behavior:

- The handler loads `last_trace.json`, appends a `user_message` event with the new message and the next `turn` value, then resumes the agent loop with the existing message history.
- A fresh **per-turn tool-call budget** applies (default 4 — see [docs/eval_agent.md](eval_agent.md) "Follow-up Mode"). `AgentTrace.tool_call_count` continues to accumulate across turns.
- The agent may call `propose_eval_case` again in a follow-up turn. When it does, the new draft **overwrites** the previous one in the response; only the latest draft is returned. Any previously persisted, human-validated `EvalCase` files under `data/eval_cases/validated/` are untouched — those are immutable once exported.
- The updated trace is atomically written back to `last_trace.json`, replacing the previous version. There is no separate per-turn trace file in v1.
- `user_intent` and `selected_step` on the trace are **not** modified by follow-up — they record the framing of the original analyze invocation only.
- v1 single-concurrency rule still applies: concurrent `/followup` calls on the same `run_id` race on `last_trace.json` and have undefined behavior.

`POST /api/eval-cases` accepts a complete `EvalCase` with
`human_validated=true` and persists it as a final regression case. The handler:

1. Validates the body against the `EvalCase` schema; returns `422` if `human_validated=false` or any required field is missing.
2. Calls `storage.save_eval_case(case)` — writes `data/eval_cases/validated/{case_id}.json`.
3. Calls `rag.upsert_eval_case(case)` — synchronously upserts into the `eval_cases` ChromaDB collection.
4. Returns the persisted `EvalCase`.

Drafts (`human_validated=false`) returned by `POST /api/runs/{run_id}/analyze`
are **not** persisted to disk in v1. They survive only in the API response and
in the trace's `propose_eval_case` tool-call args (`last_trace.json`). Page
refresh = lose the draft = re-analyze.

`GET /api/eval-cases` returns a list of persisted `EvalCase` objects by reading
`data/eval_cases/validated/*.json` (not via ChromaDB).

Search endpoint responses:

- `GET /api/failure-memory/search?q=...` returns `list[FailureMemoryCase]`.
- `GET /api/eval-cases/search?q=...` returns `list[EvalCase]` and defaults to `only_validated=true`.
- Similarity scores are not part of v1 API responses; keep scoring internal to retrieval.

Run-scoped endpoints must return `404` for an unknown `run_id`, including
`/digest`, `/preprocess`, `/analyze`, `/steps/{step_index}`, and screenshot
endpoints. Invalid `step_index` also returns `404`.

## RAG Collection Contracts

Embedding model rule:

- Collections are tied to `TRAJECTA_EMBEDDING_MODEL`. Changing the embedding model requires clearing and rebuilding persisted ChromaDB collections or writing to model-specific collection names.

Persistence directory:

- All collections live under `TRAJECTA_CHROMA_DIR` (default `data/chroma/`).
- v1 uses one ChromaDB client / one persistence directory for all collections.

Indexing model:

- Index writes are **synchronous** with the request that produces the data. No background workers in v1.
- On FastAPI startup, the failure_memory collection is hydrated from `data/failure_memory/cases.jsonl` (the seed corpus is also mirrored into the `failure_memory` SQLite table). successful_runs is hydrated by querying the `runs` table for any row with post-overlay `status == "success"` and upserting one row per such run.
- All `upsert` operations are idempotent: re-indexing the same `case_id` / `run_id` overwrites the existing row.

### `failure_memory`

Metadata:

- `case_id`
- `failure_type`
- `summary`
- `fix_hint`
- `tags`
- `source_run_id`

Text to embed:

```python
" ".join([
    failure_type,
    summary,
    fix_hint or "",
    " ".join(tags),
]).strip()
```

Seed requirements:

- `data/failure_memory/cases.jsonl` must contain at least 5 seed cases for the MVP.
- It must include at least one `missed_constraint` case because tests and demos use that retrieval path.
- Each row must validate as `FailureMemoryCase`, and all `case_id` values must be unique.

Index trigger:

- FastAPI startup: read `cases.jsonl`, validate every row, and upsert into the collection. If the collection already contains the same `case_id`, the row is overwritten (idempotent restart).
- v1 has no API endpoint to add new failure memories; the file is the source of truth.

### `eval_cases`

Metadata must preserve a complete `EvalCase`. The embedded document text may use
a retrieval-optimized subset.

Text to embed:

```text
task + failure_type + expected_behavior + actual_behavior + evidence.claim + regression_rule
```

Index trigger:

- Synchronous inside `POST /api/eval-cases`, after `storage.save_eval_case` succeeds.
- The collection only contains `human_validated=true` records — drafts are never indexed.
- FastAPI startup: rebuild the collection from `data/eval_cases/validated/*.json` if empty, for crash recovery.

### `successful_runs`

Indexes imported runs that completed successfully, so the agent can pull a
counter-example for replay-and-diff via `find_similar_successful_run`.

Metadata:

- `run_id`
- `task`
- `status` (always `"success"`; rows with other statuses are not indexed)
- `step_count`

Text to embed:

```python
task
```

Seed requirements:

- Populated at dataset-import time. Only `TrajectoryRun` records with
  `status == "success"` (after applying `run_status_overlay.json`) are indexed.
- At least one success run per fixture task category should be present so
  replay-and-diff is reachable from each demo run.
- If no successful run exists for a given task category, the tool returns an
  empty list and the agent must proceed without comparison.

Index trigger:

- Synchronous inside `POST /api/import/molmoweb-sample`: for each imported run whose post-overlay status is `"success"`, upsert one row keyed by `run_id`.
- Re-importing an existing `run_id` upserts (overwrites) the row.
- FastAPI startup: rebuild the collection from the `runs` SQLite table if empty.

### `step_summaries` (v2 placeholder)

Per-step retrieval hints (one row per `StepDigest`). **Not implemented in v1**; the trajectory digest itself fills this role through `get_run` and the agent reasons over the full digest in context. Schema and embedding text intentionally undefined here — design when the v2 use case (cross-run step retrieval at corpus scale) materializes.
