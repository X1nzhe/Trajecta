# ChromaDB RAG Design

The authoritative ChromaDB collection contracts live in
[docs/contracts.md](contracts.md#rag-collection-contracts).

v1 uses ChromaDB for text-based retrieval over failure memories, eval cases,
and successful runs (for replay-and-diff). This is **multimodal-informed RAG**,
not full image-vector multimodal RAG.

The Eval Agent reaches RAG only through tools: `search_failure_memory`,
`search_eval_cases`, and `find_similar_successful_run`. There is no implicit
retrieval on every step; the agent decides when to retrieve and what query to
use.

## Write/Read Strategy

- `failure_memory` stores reusable failure patterns. **Read-only in v1**: the seed file `data/failure_memory/cases.jsonl` is the only write path; neither the UI nor the agent can add new failure memories.
- `eval_cases` stores human-validated eval cases only (drafts are not persisted in v1).
- `successful_runs` indexes imported runs with `status == "success"` so the agent can pull a counter-example via `find_similar_successful_run` and do step-level replay-and-diff.
- `step_summaries` is a v2 placeholder; not implemented in v1.
- Store full metadata needed to reconstruct schema objects where required by the contract.
- Serialize list fields when required by the Chroma client.
- On read, deserialize metadata back into Pydantic schemas before returning through API or export code.
- `human_validated=false` drafts are not persisted in v1 (neither to disk nor to ChromaDB); only `human_validated=true` cases are written by `POST /api/eval-cases` and indexed.

## RAG Flow

1. The user triggers analysis on a run or step.
2. Trajectory Preprocessing builds or loads the trajectory digest.
3. The Eval Agent reads the digest and decides whether retrieval is needed.
4. The agent calls `search_failure_memory(query)` and/or `search_eval_cases(query)` with model-authored queries grounded in observed evidence.
5. When useful, the agent calls `find_similar_successful_run(task, exclude_run_id=current_run_id)` to retrieve a successful counter-example, then `get_run(other_run_id)` to load its digest for step-level diffing. Comparison run IDs are not part of `EvalCase.retrieved_context_ids`; the comparison is traced through `AgentTrace`.
6. Retrieved results are returned as tool observations. The agent may retrieve multiple times with refined queries.
7. The agent calls `propose_eval_case(...)` with structured evidence items and `retrieved_context_ids` populated from case IDs it actually used (failure-memory and eval-case IDs only).
8. RAGAS evaluates whether the agent's analysis is faithful to retrieved context returned from tool calls.
