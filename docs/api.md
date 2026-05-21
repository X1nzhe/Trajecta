# Backend API

Create FastAPI endpoints.

```text
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/steps/{step_index}

POST /api/import/molmoweb-sample
POST /api/runs/{run_id}/analyze
POST /api/runs/{run_id}/steps/{step_index}/analyze

GET  /api/failure-memory/search?q=...
POST /api/eval-cases
GET  /api/eval-cases
```
