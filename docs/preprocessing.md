# Trajectory Preprocessing

Trajectory Preprocessing is the stage that runs **before** the Eval Agent. It is, in spirit, **trajectory loading and validation plus low-detail orientation hints** — not failure analysis. The deterministic work (schema validation, action parsing, screenshot existence, coordinate validation) is the load-bearing part; the per-step low-detail VLM call is an *optional* orientation hint that is skipped when the source dataset already provides equivalent text. The Eval Agent owns all judgment.

It produces the **trajectory digest** — a compact, text-only summary of a trajectory run that the agent consumes as its primary input.

This document defines:

- what preprocessing does and does not do,
- how `StepDigest` and `TrajectoryDigest` from [docs/contracts.md](contracts.md#schema-contracts) are populated,
- the contract with the Eval Agent and with downstream RAG,
- how low-detail VLM is used, and how it is *not* used,
- caching, fallbacks, and tests.

## Purpose

Browser-agent trajectories can have 10–80 steps with one screenshot per step. Feeding everything into the agent at full detail is expensive and dilutes the model's attention. Preprocessing solves this in one pass:

1. Reduce every step to a small, structured text record.
2. Provide enough hint signal that the agent can form an initial hypothesis without seeing any screenshot bytes.
3. Defer all expensive visual work to the agent's `get_step_detail` tool, which fires only on suspicious steps.

The contract is intentionally one-way: preprocessing produces the digest, the agent consumes it. Preprocessing does no retrieval, no failure-labeling, and no eval-case generation.

## Scope

In scope:

- Iterate every step of a run in order.
- Parse and normalize the action.
- Validate coordinates against screenshot dimensions.
- Call a low-detail VLM (~85 tokens/image) on each screenshot, with a fixed prompt that asks for a short structural hint, **not** a free-form summary.
- Extract any text fields the source dataset already provides (URL, title, DOM/accessibility text).
- Emit a `TrajectoryDigest` containing one `StepDigest` per step.

Out of scope:

- Failure analysis or labeling — that is the Eval Agent's job.
- High-detail VLM inspection — that is the `get_step_detail` tool's job.
- RAG retrieval — the agent retrieves via tools.
- OCR — v1 does not run a separate OCR pipeline; the agent uses task-aware
  high-detail `get_step_detail` for any text or constraint-evidence reading.
- Multi-run aggregation or cross-run comparison.

## Schemas

`StepDigest` and `TrajectoryDigest` are Pydantic models defined in
[docs/contracts.md](contracts.md#schema-contracts). All preprocessing output
**must** validate against them.

Fields and how they are populated:

| Field | Source | Notes |
| --- | --- | --- |
| `index` | source trajectory | step number, zero-based |
| `action_type` | parsed from source action | one of the literals in `StepAction` |
| `action_text` | parsed from source action | human-readable, e.g. `"click at (450, 300)"` or `"type 'SFO'"` |
| `action_target` | source DOM/accessibility info if available | `None` if not present |
| `url` | source observation | `None` if not present |
| `title` | source observation | `None` if not present |
| `result_status` | source step result | `"unknown"` if not provided |
| `coord_validation_status` | computed by `coordinate_validator.py` | see [docs/dataset_import.md](dataset_import.md) |
| `vlm_low_detail_summary` | low-detail VLM call | see below |
| `has_screenshot` | `screenshots` table lookup | `True` if a `screenshots` row exists for `(run_id, StepObservation.screenshot)` |

`vlm_low_detail_summary` is **a retrieval hint, not authoritative evidence.** See [docs/eval_agent.md](eval_agent.md) "Screenshot Detail Policy".

## The Low-Detail VLM Call

A single VLM call per step at low detail (~85 tokens of image input). The fixed prompt asks for a one-line, ≤300-character hint with two segments separated by ` | `:

1. **Structured tags**: `page_type=<one of search_results / form / detail / dashboard / modal / loading / error / unknown>; modal=<yes|no>; error_banner=<yes|no>; focus=<top|center|bottom|left|right|unknown>`
2. **Visible cue**: up to ~20 words naming the most prominent legible content — hero headline, large image subject, big button label, empty-state copy, item count, or whatever visually distinguishes this page from a generic page of its type.

Example:

```
page_type=detail; modal=no; error_banner=no; focus=center | large product image, Add to Cart button visible, price prominently shown
```

The "visible cue" segment was added in `PREPROCESS_VERSION="v2"` (replacing the v1 tags-only output). The structured tags alone tended to collapse into the same string across many product / list / dashboard pages, which gave the Eval Agent no signal for picking suspicious steps. Adding ~20 words of cue lifts per-step distinguishability while keeping image input at ~85 tokens (response cost grows from ~50 to ~100 output tokens — still well below high-detail's ~1500).

A "layout changed vs the previous step" signal was considered and dropped: it would require cross-step prompting (passing the previous image or summary), which conflicts with the one-call-per-step independence of the current preprocessing loop and complicates caching. The Eval Agent can detect layout shifts from `url` / `title` changes in the digest and from `get_step_detail` deep-dives.

**It is still not allowed to transcribe small UI labels, body paragraphs, table cells, or footer links** — the resolution does not support reliable transcription, and the agent must verify any text-dependent claim via `get_step_detail`. The cue should name *what is plainly legible at a glance*, not quote arbitrary on-page strings.

### Skip Condition

If a step already has `StepObservation.visible_text` (or other reliable
DOM / accessibility text from the source dataset), the low-detail VLM call is
**skipped** for that step:

- `vlm_low_detail_summary` is set to `None`.
- The digest still records the step; downstream reasoning uses the dataset text
  in `action_target` / `visible_text`, which is preferred over any VLM hint.
- The skip decision is per-step, not per-run. A run may have a mix of
  text-rich and text-missing steps.

This keeps preprocessing cost proportional to how much the dataset already tells
us. On a run where every step has DOM text, preprocessing is purely
deterministic (no VLM calls).

## Caching

Preprocessing is idempotent for a given `(run_id, preprocess_version, preprocess_model)`. The cached digest is persisted as the `digests` row keyed by `run_id` (one row per run; `payload_json` carries the full `TrajectoryDigest`).

On API request:

1. If a `digests` row exists and its `preprocess_version` + `preprocess_model` match the current ones, return it.
2. Otherwise run preprocessing, upsert the row via `storage.save_digest`, and return the fresh digest.

Bumping `preprocess_version` in code invalidates all cached digests; this is the supported way to roll out a contract change. Re-imports also invalidate the digest — the API handler calls `storage.delete_digest(run_id)` after each `storage.save_run` because the upstream changed.

## Fallback and Offline Tests

Tests must run without network access. The preprocessing implementation looks up the VLM client through a factory:

- If `OPENAI_API_KEY` is set and the configured `TRAJECTA_VLM_MODEL` is reachable, use the real VLM.
- Otherwise, use a deterministic mock VLM that returns a fixed structural hint derived from filename, action type, and step index. The mock is sufficient for schema validation and agent-loop tests; it is not sufficient for RAGAS faithfulness scoring.

The mock path is the default for pytest. Any test that requires real VLM output must be marked and skipped when credentials are missing.

## Contract with the Eval Agent

The Eval Agent **only** sees:

- the `TrajectoryDigest` (text only), and
- tool results from `get_step_detail`, `search_failure_memory`, `search_eval_cases`.

The agent never receives raw screenshot bytes or the high-detail VLM output through the digest. This keeps the prompt small, cacheable, and within visual-token budget.

If the digest is missing or malformed, the `preprocess` node must fail fast rather than synthesize; the agent does not see a partial or stitched-together digest.

## Contract with RAG

Preprocessing **does not write to ChromaDB**. RAG ingestion of
`failure_pattern_memory`, `failure_eval_cases`, and
`successful_trajectories` is a separate concern owned by [docs/rag.md](rag.md).
The `failure_pattern_memory` concept is implemented under the collection name
`failure_memory`.

## Acceptance Criteria

- Running preprocessing on any imported run produces a `TrajectoryDigest` that validates against the schema.
- The digest contains one `StepDigest` per step in the source run, in order.
- `coord_validation_status` is set for every step that has both an action coordinate and screenshot dimensions.
- `has_screenshot` is `True` if and only if a `screenshots` row exists for `(run_id, filename)`.
- With no API key, the digest is still produced using the mock VLM and is byte-stable across runs.
- The cached `digests` row's `payload_json` round-trips through the `TrajectoryDigest` schema.
- For any step whose source `StepObservation.visible_text` is non-empty, `StepDigest.vlm_low_detail_summary` is `None` and no VLM call is recorded for that step.

## Implementation Notes

- Code lives in `backend/app/preprocess.py`.
- The VLM client factory lives in `backend/app/llm.py` (or wherever the LLM/VLM clients are centralized) and is shared with `get_step_detail`.
- Coordinate validation is delegated to `backend/app/coordinate_validator.py`.
- Action parsing is delegated to the dataset importer (`backend/app/dataset_importer.py`) and surfaced through `StepAction`.

`backend/app/preprocess.py` should expose:

```python
def build_digest(run: TrajectoryRun) -> TrajectoryDigest: ...
def load_or_build_digest(run_id: str) -> TrajectoryDigest: ...
```

`load_or_build_digest` is the entry point the API and agent use; it handles caching.

`backend/app/coordinate_validator.py` should expose:

```python
def validate_coordinates(
    action: StepAction,
    image_bytes: bytes | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
) -> CoordinateValidation: ...
```

Callers pass `image_bytes` (loaded via `storage.load_screenshot`) when the
step's image dimensions are not already known from the source row's metadata.
The validator reads PIL dimensions from the bytes only as a fallback; it
never accesses the filesystem.
