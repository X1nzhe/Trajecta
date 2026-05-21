# Dataset Import

## Dataset

Use:

```text
huggingface: allenai/MolmoWeb-HumanSkills
```

v1 should not require downloading the full dataset.

Implementation strategy:

1. Load or manually prepare a small subset.
2. Store sample rows under `data/raw/molmoweb_humanskills_sample/`.
3. Convert each selected trajectory into Trajecta JSON.
4. Copy or reference screenshots into `data/runs/{run_id}/screenshots/`.

## Status Overlay

MolmoWeb-HumanSkills does not reliably mark every run as `success` / `failed`,
but Trajecta's replay-and-diff feature requires that at least one run per
fixture task category be tagged `success`. To bridge this, the import step
applies a hand-curated overlay:

```text
data/raw/molmoweb_humanskills_sample/run_status_overlay.json
```

Schema:

```json
{
  "<run_id>": "success" | "failed" | "unknown"
}
```

Rules:

- The overlay is the **final word** on `TrajectoryRun.status`: when an entry
  exists, the imported `TrajectoryRun.status` field takes the overlay value,
  regardless of what the source dataset provided.
- Runs without an overlay entry keep whatever status the source data implies,
  falling back to `"unknown"`.
- The overlay must cover at least one `"success"` run for every fixture task
  category, otherwise `find_similar_successful_run` returns empty for those
  tasks and the demo loses replay-and-diff coverage.
- The overlay is committed to the repo as part of the fixture set. It is not
  inferred from the Eval Agent's analysis (see
  [docs/contracts.md](contracts.md#schema-contracts) note on `TrajectoryRun.status`).

## Re-Import Behavior

`POST /api/import/molmoweb-sample` is **idempotent**:

- For each imported run, `storage.save_run(run)` overwrites
  `data/runs/{run_id}/trajectory.json` atomically. Existing `digest.json`
  for the same `run_id` is invalidated (deleted) since the upstream changed.
- Existing `last_trace.json` is preserved — the user's most recent analysis
  is not destroyed by re-import.
- ChromaDB rows in the `successful_runs` collection keyed by `run_id` are
  upserted (overwritten). Rows for runs that drop out of `status=="success"`
  in the new import are deleted to avoid stale comparison targets.
- Screenshots under `data/runs/{run_id}/screenshots/` are overwritten file-by-file;
  files that exist on disk but not in the new import are left in place (no orphan deletion in v1).

## Run ID

`run_id` is the dataset's `sample_id`, copied through unchanged. Trajecta does not invent its own ID format. Because the value flows into filesystem paths (`data/runs/{run_id}/`) and URLs (`/api/runs/{run_id}/...`), the importer rejects any `sample_id` that does not match `^[A-Za-z0-9_.-]{1,128}$` and fails the import early rather than silently sanitizing.

## Importer Surface

`backend/app/dataset_importer.py` exposes:

```python
def import_sample(source_dir: Path) -> list[TrajectoryRun]: ...
def normalize_trajectory(raw: dict, run_id: str) -> TrajectoryRun: ...
def parse_action(raw_action: str | dict) -> StepAction: ...
def apply_status_overlay(runs: list[TrajectoryRun], overlay_path: Path) -> list[TrajectoryRun]: ...
```

`import_sample` is the entry point used by `POST /api/import/molmoweb-sample`: it walks `source_dir`, calls `normalize_trajectory` per sample, applies the status overlay, and returns the resulting runs. Persistence to disk and ChromaDB upserts are the caller's responsibility (see the API handler and the Index trigger rules in [docs/contracts.md](contracts.md#rag-collection-contracts)).

## Important Dataset Risk

MolmoWeb-HumanSkills may provide actions and coordinates, but coordinates must not be blindly trusted.

Before drawing overlays in the UI:

- Check whether screenshot exists.
- Read screenshot width and height.
- Parse action coordinates if available.
- Validate that coordinates fall within screenshot bounds.
- Mark coordinate status as `validated`, `out_of_bounds`, `missing`, or `unknown`.

If coordinates are not valid, UI should still show the screenshot and action text, but should not draw a misleading marker.
