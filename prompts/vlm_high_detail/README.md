# VLM High-Detail Prompt Versions

Each subdirectory is an immutable prompt version used by
`backend.app.llm.RealVLMClient.summarize_high_detail`.

Runtime selection is controlled by
`TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION`; when unset, Trajecta uses
`v1_task_context`.

Rules:

- Do not edit an existing version after it has been used for an eval run.
- Create a new directory for every high-detail VLM prompt change, for example
  `v2_result_constraints/`.
- Keep one `prompt.md` file in each version directory.
- `get_step_detail(image_detail="high")` tool results include
  `vlm_prompt_version` and `vlm_prompt_sha256`, so persisted traces can be
  reproduced and rolled back.
