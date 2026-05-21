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

## Important Dataset Risk

MolmoWeb-HumanSkills may provide actions and coordinates, but coordinates must not be blindly trusted.

Before drawing overlays in the UI:

- Check whether screenshot exists.
- Read screenshot width and height.
- Parse action coordinates if available.
- Validate that coordinates fall within screenshot bounds.
- Mark coordinate status as `validated`, `out_of_bounds`, `missing`, or `unknown`.

If coordinates are not valid, UI should still show the screenshot and action text, but should not draw a misleading marker.
