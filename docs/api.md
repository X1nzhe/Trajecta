# Backend API

Create FastAPI endpoints.

```text
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/steps/{step_index}
GET  /api/runs/{run_id}/screenshots/{filename}

POST /api/import/molmoweb-sample
POST /api/runs/{run_id}/analyze
POST /api/runs/{run_id}/steps/{step_index}/analyze

GET  /api/failure-memory/search?q=...
POST /api/eval-cases
GET  /api/eval-cases
```

## Screenshot Access

Screenshots are stored under `data/runs/{run_id}/screenshots/`.

Expose screenshots through a guarded FastAPI file endpoint:

```text
GET /api/runs/{run_id}/screenshots/{filename}
```

Behavior:

- Return the image file with the correct media type.
- Return `404` if the run or screenshot file does not exist.
- Reject path traversal; `filename` must resolve inside `data/runs/{run_id}/screenshots/`.
- Do not expose raw absolute filesystem paths to the frontend.

`StepObservation.screenshot` should contain a screenshot filename or run-relative
path. API responses may add a derived frontend URL such as:

```text
/api/runs/{run_id}/screenshots/{filename}
```
