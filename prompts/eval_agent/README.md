# Eval Agent Prompt Versions

Each subdirectory is an immutable prompt version used by
`backend.app.prompts`. Runtime selection is controlled by
`TRAJECTA_PROMPT_VERSION`; when unset, Trajecta uses `v1_minimal`.

Rules:

- Do not edit an existing version after it has been used for an eval run.
- Create a new directory for every prompt change, for example
  `v2_success_rubric/`.
- Keep both `system.md` and `followup.md` in each version directory.
- Compare versions by running `python -m backend.app.agent_eval`; reports and
  persisted traces include `prompt_version` and `prompt_sha256`.
- Roll back by setting `TRAJECTA_PROMPT_VERSION` to an older committed version.
