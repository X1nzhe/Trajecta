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

## One-Command Pipeline

For routine sample preparation, run the full local pipeline:

```bash
python3 scripts/run_molmoweb_pipeline.py
```

Default behavior:

- prefilter a `clean` candidate pool with `max_unknown_action_ratio=0.4`;
- prefilter an `anomaly` candidate pool with `unknown_action_ratio > 0.4`
  and `max_unknown_action_ratio=1.0`;
- merge both candidate ID lists;
- materialize `clean`, `anomaly`, and merged selected rows separately;
- write local Parquet copies;
- export separate `clean`, `anomaly`, and merged screenshot galleries.

Common overrides:

```bash
python3 scripts/run_molmoweb_pipeline.py \
  --max-rows 5000 \
  --target-candidates 80 \
  --materialize-limit 80
```

Use `--skip-clean`, `--skip-anomaly`, `--skip-materialize`, or
`--skip-gallery` to resume part of the pipeline. Use `--dry-run` to print the
underlying commands without downloading data.

The clean and anomaly pools are intended to be mutually exclusive. If you have
old outputs created before this rule existed, rerun the prefilter step instead
of reusing the old `data/raw/molmoweb_humanskills_candidates/anomaly/` files.

Pipeline outputs:

```text
data/raw/molmoweb_humanskills_candidates/
  clean/
  anomaly/
  selected_sample_ids.txt
data/raw/molmoweb_humanskills_sample/
  pools/clean/hf_dataset/
  pools/clean/image_gallery/index.html
  pools/anomaly/hf_dataset/
  pools/anomaly/image_gallery/index.html
  hf_dataset/                # merged clean + anomaly subset
  image_gallery/index.html   # merged gallery
```

## Source Dataset Structure

The upstream Hugging Face dataset card defines the raw row shape for
`allenai/MolmoWeb-HumanSkills`. Treat this section as the source-side import
contract only; Trajecta's normalized Pydantic schemas remain defined in
[docs/contracts.md](contracts.md#schema-contracts).

Source: <https://huggingface.co/datasets/allenai/MolmoWeb-HumanSkills>

Raw row fields:

| Field | Type | Import meaning |
| --- | --- | --- |
| `sample_id` | `string` | Stable trajectory identifier. Trajecta copies this to `TrajectoryRun.run_id` after validating the run ID pattern. |
| `instruction` | `string` | JSON-encoded task instruction. Prefer `low_level` when present for `TrajectoryRun.task`; otherwise fall back to another readable instruction field. |
| `trajectory` | `string` | JSON-encoded dict keyed by step index. Each value contains the step screenshot reference, action, browser observation, and timestamp. |
| `images` | `list[bytes]` / `list[Image]` | Source of truth for screenshot content. Do not store this field inside normalized JSON; write selected screenshots to disk. |
| `image_paths` | `list[path]` or `null` | Optional screenshot filenames aligned with `images`. Observed materialized rows may have `null`, so importer logic must not require this field. |

Raw `trajectory` step fields:

| Field | Type | Import meaning |
| --- | --- | --- |
| `screenshot` | `string` | Desired screenshot filename for the step, for example `screenshot_001.png`. Store only the run-relative filename in `StepObservation.screenshot`. |
| `action` | `dict` | Agent action record. Parse `action_str`, preserve useful source details in metadata, and normalize to `StepAction`. |
| `other_obs` | `dict` | Browser observation state, including URL, page index, open page titles, and open page URLs when available. |
| `action_timestamp` | `float` | Unix timestamp for the action. Convert to the normalized timestamp representation used by `TrajectoryStep.timestamp`. |

Screenshot extraction rule:

- Always read screenshot bytes from `images`.
- If `image_paths` is a non-empty list, first try matching
  `trajectory[step].screenshot` against `image_paths`.
- If `image_paths` is `null` or empty, map `images[i]` to the screenshot
  reference from the `i`th trajectory step after sorting step keys numerically.
- If the number of images and screenshot references differs, import what can be
  mapped and mark missing screenshots as unavailable downstream.

```python
import json

row = ds[0]
traj = json.loads(row["trajectory"])
steps = [traj[key] for key in sorted(traj.keys(), key=int)]

if row.get("image_paths"):
    image_by_name = dict(zip(row["image_paths"], row["images"]))
else:
    image_by_name = {
        step["screenshot"]: image
        for step, image in zip(steps, row["images"])
        if step.get("screenshot")
    }

for step in steps:
    screenshot_name = step.get("screenshot")
    if not screenshot_name:
        continue
    img = image_by_name.get(screenshot_name)
```

If `img` is missing, import the step but mark screenshot availability as
unavailable downstream. Missing screenshots are non-fatal in v1.

## Automatic Prefilter

Do not manually inspect the full dataset. First run a metadata-only streaming
prefilter to produce a small candidate list:

```bash
pip install datasets python-dotenv
# Optional, but recommended for higher Hugging Face rate limits:
# put HF_TOKEN=... in the project root .env, or export it in the shell.

python3 scripts/prefilter_molmoweb.py \
  --max-rows 2000 \
  --target-candidates 50 \
  --min-steps 3 \
  --max-steps 30
```

The script uses `load_dataset(..., streaming=True)` and selects only:

```text
sample_id, instruction, trajectory, image_paths
```

Before connecting to Hugging Face, the script loads a dotenv-style file from
`.env` by default. Existing shell environment variables take precedence. It
passes `HF_TOKEN` to Hugging Face when that environment variable is available.
If you use `huggingface-cli login` instead, pass `--use-cached-token`. The token
is not written to the output summary.

For a quick connectivity smoke test, use a relaxed step threshold because the
first rows may contain many one-step "go to" trajectories:

```bash
python3 scripts/prefilter_molmoweb.py \
  --max-rows 20 \
  --target-candidates 5 \
  --min-steps 1
```

The prefilter does not materialize screenshots or download the full dataset. It writes:

```text
data/raw/molmoweb_humanskills_candidates/
  candidates.jsonl
  candidate_sample_ids.txt
  summary.json
```

Prefilter acceptance rules:

- `sample_id` must match the Trajecta run ID filesystem contract.
- `trajectory` and `instruction` must parse.
- step count must be within the configured range.
- trajectory step keys must be numeric.
- trajectory steps must contain screenshot references.
- action strings must be mostly classifiable as `click`, `type`, `scroll`,
  `navigate`, or `wait`.

If `image_paths` is missing, empty, or invalid in a streamed metadata row, the
candidate is not rejected by default. The candidate records `image_path_status`
and leaves `screenshot_coverage` as `null`; the later materialization step
verifies screenshot bytes using the full row's `images` column. If `image_paths`
is present, the prefilter records match coverage for diagnostics but does not
reject by default. Use `--require-image-paths --require-image-path-match` only
when intentionally auditing the upstream `image_paths` field.

The output is still only a candidate set. A human should review the final
`5-20` selected runs for demo value, then write:

```text
data/raw/molmoweb_humanskills_sample/run_status_overlay.json
```

Coordinate bounds are validated later during import/preprocessing after
screenshots have been materialized locally.

## Materialize Selected Rows

Do not search Hugging Face by `sample_id`. `sample_id` is a row value inside the
dataset, not a Hub-indexed artifact ID, so Hub search is not a reliable lookup
mechanism. To download the selected rows, stream the dataset again and filter
locally:

```bash
python3 scripts/materialize_molmoweb_sample.py \
  --sample-id-file data/raw/molmoweb_humanskills_candidates/candidate_sample_ids.txt \
  --max-rows 2000 \
  --output-dir data/raw/molmoweb_humanskills_sample/hf_dataset
```

Use the same or larger `--max-rows` value that produced the candidate file. If
you omit `--max-rows` or pass `--max-rows 0`, the script scans until all
requested IDs are found or the stream ends.

The materializer loads `.env` and passes `HF_TOKEN` using the same rules as the
prefilter. It keeps all source row fields, including `images`, and saves the
result as both a local Hugging Face Dataset and a Parquet file with the same
row fields:

```python
from datasets import load_dataset, load_from_disk

ds = load_from_disk("data/raw/molmoweb_humanskills_sample/hf_dataset")
row = ds[0]

parquet_ds = load_dataset(
    "parquet",
    data_files="data/raw/molmoweb_humanskills_sample/molmoweb_humanskills_sample.parquet",
    split="train",
)
```

Outputs:

```text
data/raw/molmoweb_humanskills_sample/
  hf_dataset/                         # Dataset.save_to_disk output
  molmoweb_humanskills_sample.parquet # local Parquet subset
  materialize_manifest.json           # requested/found/missing IDs and scan settings
```

Pass `--overwrite` to replace existing materialized outputs. Pass
`--parquet-path ""` to skip Parquet export. Missing IDs are reported in
`materialize_manifest.json`; increase `--max-rows` if the IDs came from a
longer prefilter pass.

## View Materialized Screenshots

After materializing selected rows, export screenshot bytes to normal image files
and generate a local HTML gallery:

```bash
python3 scripts/export_molmoweb_images.py \
  --input-dir data/raw/molmoweb_humanskills_sample/hf_dataset \
  --output-dir data/raw/molmoweb_humanskills_sample/image_gallery \
  --overwrite
```

Open:

```text
data/raw/molmoweb_humanskills_sample/image_gallery/index.html
```

Each gallery section shows the full `sample_id`, the parsed task instruction,
and the exported screenshots for that trajectory.

By default, the exporter writes only images referenced by the row's
`trajectory[*].screenshot` values when those references are available. Pass
`--all-images` to export every image in each materialized row.

Some streamed rows may have `image_paths == null` even though `images` contains
valid image bytes. In that case, the exporter derives filenames from
`trajectory` screenshot references by step order and exports the images instead
of filtering them out.

The gallery also records screenshot diagnostics. Each image caption includes
dimensions, byte size, and quality flags. A `manifest.json` is written next to
`index.html`:

```text
data/raw/molmoweb_humanskills_sample/image_gallery/manifest.json
```

Flags:

- `low_bytes`: image file is under 20 KB and is unlikely to be a useful full-page screenshot.
- `small_dimensions`: image width is under 600px or height is under 400px.
- `low_pixel_count`: total pixel count is under 300,000.
- `unknown_dimensions`: dimensions could not be parsed from the image bytes.

Runs dominated by flagged screenshots should usually be excluded from the
committed fixture set.

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
