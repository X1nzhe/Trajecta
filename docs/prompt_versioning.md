# Prompt Versioning

Trajecta treats Eval Agent prompts, high-detail VLM prompts, and Phase 8
judge prompts as versioned repo artifacts.

## Storage

Prompt versions live under:

```text
prompts/eval_agent/<version>/
  system.md
  followup.md

prompts/vlm_high_detail/<version>/
  prompt.md

prompts/judge/<version>/
  prompt.md
```

The default version is `v1_minimal`. Runtime selection is controlled by:

```bash
TRAJECTA_PROMPT_VERSION=v1_minimal
TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION=v1_task_context
```

When variables are unset, the backend uses `v1_minimal` for the Eval Agent and
`v1_task_context` for high-detail VLM inspection.

The committed Eval Agent prompt bundles are `v1_minimal`, `v2_success_rubric`,
`v3_balanced_rubric`, `v4_search_strategy_rubric`, `v5_constraint_verification`,
and `v6_guided_autonomy`. `v6_guided_autonomy` is the current featured prompt:
it pairs a legible per-tool contract and explicit investigation freedom with a
strict verdict/evidence contract (burden-of-proof, `not_visible` handling that
distinguishes a missing screenshot from an unenforced constraint). See the
README "Prompt Iteration" table for per-version metrics.

Judge prompt versions are selected by the Phase 8 judge runner, not by
the Eval Agent runtime. The Phase 8 production judge path uses two LLM
judge configs over the same `agent_eval` artifact set:

- Judge A: Gemini-compatible provider/model configured by
  `TRAJECTA_JUDGE_A_MODEL`.
- Judge B: OpenAI-compatible provider/model configured by
  `TRAJECTA_JUDGE_B_MODEL`.
- Judge prompt versions configured by `TRAJECTA_JUDGE_A_PROMPT_VERSION` and
  `TRAJECTA_JUDGE_B_PROMPT_VERSION`.

No Gemini or OpenAI model ID is hard-coded as a repo default. Operators choose
the concrete model IDs for each run.

The repository ships four judge prompt bundles:

- `prompts/judge/v1_acceptability/` — the shared baseline rubric.
- `prompts/judge/v1_acceptability_gemini/` (Phase 8 A4.2) — the
  Gemini-flavored Judge A bundle. Same six required assertions, same
  verdict vocabulary, and same `"acceptable" iff every assertion passes`
  rule as the baseline. The response-format instructions are tuned for
  Gemini's tendency to wrap output in markdown code fences (the bundle
  explicitly demands raw JSON, no preamble, no backticks).
- `prompts/judge/v1_acceptability_openai/` (Phase 8 A4.2) — the
  OpenAI-flavored Judge B bundle. Same rubric semantics; response-format
  instructions emphasize strict JSON parsability and concise rationales.
- `prompts/judge/v2_strict_assertions/` — archived / experimental. Not
  part of the Phase 8 mandatory path; not required for the Gemini /
  OpenAI production pair.

The two `v1_acceptability_*` bundles **must preserve the same
acceptability rubric semantics**: identical assertion names
(`verdict_alignment`, `failure_mode_compatibility`,
`failure_step_localization`, `regression_case_usefulness`,
`no_forbidden_claim`, `evidence_support`), identical verdict vocabulary
(`"acceptable"` / `"unacceptable"`), identical status vocabulary
(`"pass"` / `"fail"`), and the same rule that `verdict: "acceptable"`
requires every assertion to pass. Only the provider-specific
response-format instructions and wording may differ. Phase 8 computes
κ_LLM,LLM between the two judge verdict streams; rubric drift would
turn that disagreement into a prompt-content artefact rather than a
genuine model-vs-model signal.

No Gemini or OpenAI model ID is hard-coded as a repo default. Operators
choose `TRAJECTA_JUDGE_<slot>_MODEL` per run; the judge prompt bundle
is selected independently via `TRAJECTA_JUDGE_<slot>_PROMPT_VERSION`,
so the same prompt bundle can in principle drive multiple model
choices, and an operator may also point both slots at the same bundle
if a shared-rubric ablation is desired.

## Rules

- Never edit a prompt version after it has been used for an eval run.
- Create a new directory for every prompt change, such as
  `prompts/eval_agent/v2_success_rubric/` or
  `prompts/vlm_high_detail/v2_result_constraints/` or
  `prompts/judge/v2_strict_assertions/`.
- If `prompts/judge/v2_strict_assertions/` exists, treat it as an
  archived / experimental prompt. It is not required for Phase 8
  acceptance and is not required for the Gemini/OpenAI production pair.
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

Every judge report records, per LLM judge run:

- `judge_model`
- `judge_prompt_version`
- `judge_prompt_sha256`

`python -m backend.app.agent_eval` also writes the Eval Agent prompt fields
into `eval/agent_report.{json,md}` and timestamped reports under
`eval/runs/`. Eval reports also include
`vlm_high_detail_prompt_version` and `vlm_high_detail_prompt_sha256`.

## Failure Memory Changes

Failure memory source of truth remains
`data/failure_memory/cases.jsonl`. On startup or manual eval, `rag.hydrate_all`
rebuilds the ChromaDB `failure_pattern_memory` collection from that file
(implementation collection: `failure_memory`), so removed or renamed memory
cases do not leave stale vectors behind.

SQLite rows are also rebuilt from `cases.jsonl` by `storage.load_failure_memory`.
If you change the embedding model, still clear `data/chroma/` or point
`TRAJECTA_CHROMA_DIR` at a fresh directory because embeddings are not migrated
between models.
