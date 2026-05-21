# Backend API

The authoritative endpoint list and response contracts live in
[docs/contracts.md](contracts.md#api-contracts).

Implementation notes:

- Create FastAPI routes matching the API contract.
- `POST /api/runs/{run_id}/analyze` runs the Eval Agent and returns an `EvalCase` draft plus `AgentTrace`.
- `POST /api/runs/{run_id}/preprocess` returns the cached or freshly built `TrajectoryDigest`.
- Calling `/analyze` should preprocess on demand if the digest is missing or stale.
- `POST /api/runs/{run_id}/followup` continues the existing `last_trace.json` with one additional turn. See [docs/contracts.md "Follow-up Contract"](contracts.md#follow-up-contract) for preconditions, request shape, and overwrite semantics. The endpoint reuses the same handler scaffolding as `/analyze` but skips the preprocess node and rehydrates `messages` from the persisted trace.

## Screenshot Access

Screenshots are stored under `data/runs/{run_id}/screenshots/` and served by:

```text
GET /api/runs/{run_id}/screenshots/{filename}
```

Security and path rules are defined in
[docs/contracts.md](contracts.md#screenshot-contract).
