# Data Model

The authoritative Pydantic schema contract lives in
[docs/contracts.md](contracts.md#schema-contracts).

Implementation target:

- Create `backend/app/schemas.py`.
- Keep `backend/app/schemas.py` aligned with `docs/contracts.md`.
- Do not redefine schema fields in this file; update `docs/contracts.md` first.

Model families:

- Trajectory data: `Coordinate`, `BBox`, `StepAction`, `StepObservation`, `StepResult`, `CoordinateValidation`, `TrajectoryStep`, `Trajectory`
- Preprocessing output: `StepDigest`, `TrajectoryDigest`
- Memory and eval artifacts: `FailureMemoryCase`, `EvidenceItem`, `EvalCase`
- Agent observability: `AgentTraceEvent`, `AgentTrace`

Related behavior docs:

- [docs/preprocessing.md](preprocessing.md) explains how `StepDigest` and `TrajectoryDigest` are populated.
- [docs/eval_agent.md](eval_agent.md) explains how `EvalCase` drafts and `AgentTrace` are produced.
- [docs/rag.md](rag.md) explains how schema objects are stored and retrieved through ChromaDB.
