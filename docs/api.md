# Backend API

The authoritative endpoint list and response contracts live in
[docs/contracts.md](contracts.md#api-contracts).

Implementation notes:

- Create FastAPI routes matching the API contract.
- `POST /api/trajectories/{trajectory_id}/analyze` runs the Eval Agent and **streams** an NDJSON response (`application/x-ndjson`) of `event` / `done` / `error` lines. Use `fastapi.responses.StreamingResponse` over an async generator that wraps LangGraph's `astream` and yields one JSON line per appended trace event, then a terminal `done` line with the complete trace and draft.
- `POST /api/trajectories/{trajectory_id}/preprocess` returns the cached or freshly built `TrajectoryDigest` (regular JSON, not streamed).
- Calling `/analyze` should preprocess on demand if the digest is missing or stale.
- `POST /api/trajectories/{trajectory_id}/followup` continues the existing persisted trace (the `traces` row keyed by `trajectory_id`) with one additional turn and uses the **same NDJSON stream format** as `/analyze`. See [docs/contracts.md "Follow-up Contract"](contracts.md#follow-up-contract) for preconditions, request shape, and overwrite semantics. The endpoint reuses the same streaming scaffolding as `/analyze` but skips the preprocess node and rehydrates `messages` from the persisted trace.
- HTTP error responses (4xx/5xx) are **not** streamed — they return a normal JSON error body so generic FastAPI exception handlers continue to work.

## Screenshot Access

Screenshots live as BLOB rows in the `screenshots` SQLite table (keyed by `(trajectory_id, filename)`) and are streamed by:

```text
GET /api/trajectories/{trajectory_id}/screenshots/{filename}
```

Security and path rules are defined in
[docs/contracts.md](contracts.md#screenshot-contract).
