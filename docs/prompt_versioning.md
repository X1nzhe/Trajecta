# Prompt Versioning

Trajecta treats Eval Agent prompts and high-detail VLM prompts as versioned
repo artifacts.

## Storage

Prompt versions live under:

```text
prompts/eval_agent/<version>/
  system.md
  followup.md

prompts/vlm_high_detail/<version>/
  prompt.md
```

The default version is `v1_minimal`. Runtime selection is controlled by:

```bash
TRAJECTA_PROMPT_VERSION=v1_minimal
TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION=v1_task_context
```

When variables are unset, the backend uses `v1_minimal` for the Eval Agent and
`v1_task_context` for high-detail VLM inspection.

## Rules

- Never edit a prompt version after it has been used for an eval run.
- Create a new directory for every prompt change, such as
  `prompts/eval_agent/v2_success_rubric/` or
  `prompts/vlm_high_detail/v2_result_constraints/`.
- Keep prompt changes small enough to compare against the prior version with
  `python -m backend.app.agent_eval`.
- Roll back by setting the corresponding environment variable to a previous
  committed version.

## Traceability

Every new `AgentTrace` records:

- `prompt_version`
- `prompt_sha256`

Every high-detail `get_step_detail` tool result records:

- `vlm_prompt_version`
- `vlm_prompt_sha256`

`python -m backend.app.agent_eval` also writes the Eval Agent prompt fields
into `eval/agent_report.{json,md}` and timestamped reports under
`eval/runs/`. Eval reports also include
`vlm_high_detail_prompt_version` and `vlm_high_detail_prompt_sha256`.

## Failure Memory Changes

Failure memory source of truth remains
`data/failure_memory/cases.jsonl`. On startup or manual eval, `rag.hydrate_all`
rebuilds the ChromaDB `failure_memory` collection from that file, so removed or
renamed memory cases do not leave stale vectors behind.

SQLite rows are also rebuilt from `cases.jsonl` by `storage.load_failure_memory`.
If you change the embedding model, still clear `data/chroma/` or point
`TRAJECTA_CHROMA_DIR` at a fresh directory because embeddings are not migrated
between models.
