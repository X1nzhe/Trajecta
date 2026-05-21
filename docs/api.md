# Backend API

The authoritative endpoint list and response contracts live in
[docs/contracts.md](contracts.md#api-contracts).

Implementation notes:

- Create FastAPI routes matching the API contract.
- `POST /api/runs/{run_id}/analyze` runs the Eval Agent and returns an `EvalCase` draft plus `AgentTrace`.
- `POST /api/runs/{run_id}/preprocess` returns the cached or freshly built `TrajectoryDigest`.
- Calling `/analyze` should preprocess on demand if the digest is missing or stale.

## Screenshot Access

Screenshots are stored under `data/runs/{run_id}/screenshots/` and served by:

```text
GET /api/runs/{run_id}/screenshots/{filename}
```

Security and path rules are defined in
[docs/contracts.md](contracts.md#screenshot-contract).
