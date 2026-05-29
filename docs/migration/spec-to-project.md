# SPEC.md to PROJECT.md Migration Plan

## Migration Goal

Rename the root project specification document from `SPEC.md` to `PROJECT.md`
and update all repository references that point to the file by name.

After the migration, `PROJECT.md` should be the project entry point for MVP
priorities, non-goals, document map, and Phase 8 alignment references. No
runtime behavior, API contract, schema, storage layout, or product scope should
change.

## Execution Protocol

- This migration is docs/comment-only.
- Do not change runtime behavior.
- Do not rename code symbols, APIs, schemas, fixtures, or product concepts named `spec`.
- Every changed line must be either:
  - the root file rename,
  - a direct `SPEC.md` -> `PROJECT.md` reference update,
  - the heading update in `PROJECT.md`,
  - a comment-only stale file-name reference update.
- If a change does not fit those categories, stop and record it under `Decision Needed`.

## In Scope

- Rename root `SPEC.md` to `PROJECT.md`.
- Update the document heading from `# SPEC.md` to `# PROJECT.md`.
- Update every direct `SPEC.md` file reference, including historical Phase 8
  checklist and presentation references.
- Update Markdown links and anchors that currently target `SPEC.md`, preserving
  existing section anchors when the section names stay unchanged.
- Update code and test comments that mention `SPEC.md` so the repository no
  longer carries stale file-name references.
- Verify that no direct `SPEC.md` references remain outside rollback notes or
  intentional migration-history text.

## Out of Scope

- Renaming product concepts such as "spec", "operating spec", or
  "operational spec" when they describe a document type rather than the old
  file name.
- Changing runtime code, APIs, schemas, model behavior, tests, fixtures, or
  generated reports.
- Editing generated mock HTML under `docs/mocks/**`.
- Changing unrelated occurrences such as `CSS spec`, Alembic
  `specification`, or `.gitignore` patterns like `*.spec`.
- Reworking Phase 8 scope, acceptance criteria, or architecture content beyond
  the file-name migration.
- Changing executable test behavior, assertions, fixtures, snapshots, or runtime code.
- Comment-only edits are allowed only for files explicitly listed in Affected Files.

## Affected Files

Direct file references that should be updated:

- `SPEC.md` -> `PROJECT.md`
- `AGENTS.md`
- `docs/phase8_s18_alignment.md`
- `docs/security_governance.md`
- `docs/roadmap.md`
- `docs/architecture.md`
- `docs/testing.md`
- `docs/mcp.md`
- `backend/app/schemas.py` comment only
- `backend/app/eval_agent_graph.py` comment only
- `backend/tests/test_eval_agent.py` comment only

Known non-migration matches that should remain unchanged:

- `.gitignore` `*.spec`
- `frontend/src/index.css` `CSS spec`
- `backend/alembic.ini` `specification`
- `docs/mocks/**` generated HTML `spec` / `specification` strings
- Generic `operating spec` / `operational spec` prose when not naming
  `SPEC.md`

This list is based on the audit at the time this plan was created. Before execution, rerun the verification search and update this list if new direct references appear.

## Rename Steps

1. Rename the root file with Git so history is preserved:

   ```bash
   git mv SPEC.md PROJECT.md
   ```

2. Update the first heading in `PROJECT.md`:

   ```text
   # PROJECT.md
   ```

3. Replace direct references to `SPEC.md` with `PROJECT.md` in the affected
   documentation files.

4. Update Markdown links while preserving anchors:

   ```text
   ../SPEC.md#components-used -> ../PROJECT.md#components-used
   ../SPEC.md#design-decisions -> ../PROJECT.md#design-decisions
   ```

5. Update repository tree examples so they list `PROJECT.md` instead of
   `SPEC.md`.

6. Update code and test comments that describe the cost-ablation demo as
   reading from `SPEC.md`; the wording should refer to `PROJECT.md`.

7. Leave unrelated `spec` terminology unchanged unless it directly names the
   old file.

## Verification Commands

Run the file-name checks:

```bash
rg -n --hidden --glob '!.git/**' --glob '!frontend/node_modules/**' --glob '!backend/.venv/**' --glob '!data/**' -S 'SPEC\.md|\bSPEC\b' .
```

Expected result: no stale `SPEC.md` / `SPEC` direct file-name references,
except this migration plan if retained as history.

Run the broad terminology check and manually confirm only intentional
non-migration uses remain:

```bash
rg -n --hidden --glob '!.git/**' --glob '!frontend/node_modules/**' --glob '!backend/.venv/**' --glob '!data/**' --glob '!docs/mocks/**' -S '\bspec\b|Specification|specification|specs' .
```

Confirm `PROJECT.md` is now discoverable:

```bash
test -f PROJECT.md && test ! -f SPEC.md
rg -n -S 'PROJECT\.md|\\bPROJECT\\b' AGENTS.md PROJECT.md docs backend
```

For a docs-only migration, no test suite is required. If comments in backend
files are changed, running the default backend tests is optional but low risk:

```bash
cd backend && pytest
```

Allowed remaining file-name matches:
- `docs/migration/spec-to-project.md`, as migration history only.
- Rollback examples inside this migration plan.

## Stop Conditions

Stop and ask for review if:
- A reference to `SPEC.md` appears to describe a product concept rather than the old file name.
- Updating a reference requires changing executable code.
- A generated file or snapshot appears to need updates.
- The search reveals affected files not listed in this plan.

## Rollback Notes

Rollback is mechanical:

1. Rename the file back:

   ```bash
   git mv PROJECT.md SPEC.md
   ```

2. Revert direct references from `PROJECT.md` to `SPEC.md` in the affected
   files listed above.

3. Restore Markdown links:

   ```text
   ../PROJECT.md#components-used -> ../SPEC.md#components-used
   ../PROJECT.md#design-decisions -> ../SPEC.md#design-decisions
   ```

4. Re-run the verification commands with `PROJECT.md` and `SPEC.md` swapped to
   confirm the rollback is internally consistent.

## Progress

- [x] Phase 1: Rename `SPEC.md` to `PROJECT.md`
- [x] Phase 2: Update direct references
- [x] Phase 3: Run verification commands
- [x] Phase 4: Record remaining intentional matches

Verification results:

- Direct file-name search: only this migration plan retains `SPEC.md` /
  `SPEC` as migration history and rollback instructions.
- File existence check: `PROJECT.md` exists and `SPEC.md` no longer exists.
- `PROJECT.md` discoverability search: root document, AGENTS, affected docs,
  and comment-only backend references now point to `PROJECT.md`.
- Broad `spec` terminology search: remaining matches are intentional
  non-migration concepts or ignore/generated/tooling patterns documented
  above.
- Full backend `pytest`: failed only because the opt-in real LLM integration
  test attempted an OpenAI network call and raised `APIConnectionError`.
- Offline backend pytest subset, excluding real LLM integration:
  `237 passed, 1 skipped, 1 deselected, 1 warning`.
