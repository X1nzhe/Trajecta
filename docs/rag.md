# ChromaDB RAG Design

The authoritative ChromaDB collection contracts live in
[docs/contracts.md](contracts.md#rag-collection-contracts).

v1 uses ChromaDB for text-based retrieval over failure pattern memory, failure
EvalCases, and successful trajectories (for replay-and-diff). This is
**multimodal-informed RAG**, not full image-vector multimodal RAG.

Canonical conceptual collection names:

- `failure_pattern_memory`: hand-written seed memory for reusable failure
  patterns.
- `failure_eval_cases`: failure-shaped, human-validated EvalCases used as
  similar-failure precedents.
- `successful_trajectories`: trajectories whose success was human-validated by
  a success-shaped EvalCase, used for replay-and-diff.

Chroma collection names in code: `failure_eval_cases` and
`successful_trajectories` match the conceptual names above. The
`failure_pattern_memory` concept is implemented under the stable name
`failure_memory` — the same name as its SQLite table, seed file, and
`search_failure_memory` tool — mirroring how `trajectory` is implemented as
`run`.

`failure_pattern_memory` (implementation collection: `failure_memory`) is
rebuilt from `data/failure_memory/cases.jsonl` during `rag.hydrate_all()`, so
removed or renamed seed cases do not leave stale Chroma vectors behind.
Changing `TRAJECTA_EMBEDDING_MODEL` still requires clearing `data/chroma/` or
using a fresh `TRAJECTA_CHROMA_DIR`, because embeddings are not migrated
between models.

The Eval Agent reaches RAG only through tools: `search_failure_memory`,
`search_eval_cases`, and `find_similar_successful_run`. There is no implicit
retrieval on every step; the agent decides when to retrieve and what query to
use.

## Write/Read Strategy

- `failure_pattern_memory` stores reusable failure patterns. **Read-only in v1**: the seed file `data/failure_memory/cases.jsonl` is the only write path; neither the UI nor the agent can add new failure pattern memories. Implementation collection name: `failure_memory`.
- `failure_eval_cases` stores failure-shaped, human-validated EvalCases only. Success-shaped EvalCases are persisted in SQLite `eval_cases`, but they do not belong in this failure-precedent retrieval index.
- `successful_trajectories` indexes trajectories that humans have validated as successful via `POST /api/eval-cases` (success-shape `EvalCase`). The collection starts **empty** after `Import Dataset` and grows only as users validate success verdicts. `find_similar_successful_run` uses it to pull a counter-example for step-level replay-and-diff. Imports never seed this collection directly (cold-start contract).
- `step_summaries` is a v2 placeholder; not implemented in v1.
- Store full metadata needed to reconstruct schema objects where required by the contract.
- Serialize list fields when required by the Chroma client.
- On read, deserialize metadata back into Pydantic schemas before returning through API or export code.
- `human_validated=false` drafts are not persisted in v1 (neither to disk nor to ChromaDB); only `human_validated=true` cases are written by `POST /api/eval-cases` and indexed.

## RAG Flow

1. The user triggers analysis on a trajectory (legacy API wording: run) or step.
2. Trajectory Preprocessing builds or loads the trajectory digest.
3. The Eval Agent reads the digest and decides whether retrieval is needed.
4. The agent calls `search_failure_memory(query)` and/or `search_eval_cases(query)` with model-authored queries grounded in observed evidence.
5. When useful, the agent calls `find_similar_successful_run(task, exclude_run_id=current_run_id)` to retrieve a successful trajectory counter-example, then `get_run(other_run_id)` to load its digest for step-level diffing. Comparison run IDs are not part of `EvalCase.retrieved_context_ids`; the comparison is traced through `AgentTrace`.
6. Retrieved results are returned as tool observations. The agent may retrieve multiple times with refined queries.
7. The agent calls `propose_eval_case(...)` with structured evidence items and `retrieved_context_ids` populated from case IDs it actually used (failure-memory and eval-case IDs only).
8. RAGAS evaluates whether the agent's analysis is faithful to retrieved context returned from tool calls.
