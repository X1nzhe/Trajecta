#!/usr/bin/env python3
"""Materialize the human-curated MolmoWeb demo fixture from a triage CSV.

Workflow:
  1. Read sample_id and outcome from the triage CSV.
  2. Write demo_sample_ids.txt and run_status_overlay.json next to the sample root.
  3. Stream the Hugging Face dataset to materialize those rows with images.
  4. Export a local HTML gallery for visual QA.

The triage CSV is the human source of truth. Re-running this script is idempotent:
it overwrites the derived artifacts but never touches the CSV itself.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_TRIAGE_CSV = "data/triage_notes.csv"
DEFAULT_SAMPLE_ROOT = "data/raw/molmoweb_humanskills_sample"
VALID_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
VALID_OUTCOMES = {"success", "failed", "unknown"}


def read_triage_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"triage CSV does not exist: {path}")

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, restval="")
        required = {"sample_id", "outcome"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"triage CSV is missing required columns: {sorted(missing)}")

        for line_no, raw in enumerate(reader, start=2):
            sample_id = (raw.get("sample_id") or "").strip()
            if not sample_id:
                continue
            if not VALID_RUN_ID_RE.fullmatch(sample_id):
                raise ValueError(f"line {line_no}: invalid sample_id {sample_id!r}")
            if sample_id in seen:
                raise ValueError(f"line {line_no}: duplicate sample_id {sample_id!r}")
            seen.add(sample_id)

            outcome = (raw.get("outcome") or "").strip().lower()
            if outcome and outcome not in VALID_OUTCOMES:
                raise ValueError(
                    f"line {line_no}: outcome must be one of {sorted(VALID_OUTCOMES)} "
                    f"(got {outcome!r})"
                )

            rows.append(
                {
                    "sample_id": sample_id,
                    "category": (raw.get("category") or "").strip(),
                    "outcome": outcome,
                    "failure_mode": (raw.get("failure_mode") or "").strip(),
                    "notes": (raw.get("notes") or "").strip(),
                }
            )
    return rows


def read_triage_csvs(paths: list[Path]) -> list[dict[str, str]]:
    """Read and merge multiple triage CSVs into one de-duplicated union.

    Each file is parsed with ``read_triage_csv``, which already enforces the
    per-file schema, the sample_id pattern, the outcome vocabulary, and
    intra-file dedup. A sample_id appearing in more than one file is a hard
    error: it would silently mix annotation sets that must stay disjoint (e.g.
    the golden test set in ``triage_notes.csv`` and the HITL eval-case seed
    set in ``hitl_candidate_notes.csv``), which is exactly the leakage we want
    to prevent. Rows are concatenated in argument order, so the output is
    deterministic.
    """
    merged: list[dict[str, str]] = []
    origin: dict[str, Path] = {}
    for path in paths:
        for row in read_triage_csv(path):
            sample_id = row["sample_id"]
            if sample_id in origin:
                raise ValueError(
                    f"duplicate sample_id {sample_id!r} across triage CSVs: "
                    f"present in both {origin[sample_id]} and {path}"
                )
            origin[sample_id] = path
            merged.append(row)
    return merged


def write_sample_ids(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(row["sample_id"] for row in rows) + "\n", encoding="utf-8")


def write_status_overlay(rows: list[dict[str, str]], path: Path) -> None:
    overlay = {row["sample_id"]: row["outcome"] for row in rows if row["outcome"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def category_summary(rows: list[dict[str, str]]) -> str:
    by_category: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_category.setdefault(row["category"] or "(uncategorized)", {})
        key = row["outcome"] or "unknown"
        bucket[key] = bucket.get(key, 0) + 1
    lines = []
    for category in sorted(by_category):
        counts = by_category[category]
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        lines.append(f"  {category}: {parts}")
    return "\n".join(lines)


def run_subprocess(cmd: list[str], dry_run: bool) -> None:
    print("\n$ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def assert_materialization_complete(manifest_path: Path, expected: int) -> None:
    """Fail loudly if any requested sample_id was not materialized.

    ``materialize_molmoweb_sample.py`` only *warns* on missing IDs and still
    exits 0, so a typo'd or nonexistent sample_id is silently dropped from
    ``hf_dataset/`` (it lands in the manifest's ``missing_sample_ids``). The
    run then never imports and never shows in the frontend — while a row-count
    check on ``demo_sample_ids.txt`` still passes. Turn that into a hard error.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(f"materialize manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = manifest.get("missing_sample_ids") or []
    if missing:
        raise SystemExit(
            f"materialization incomplete: {len(missing)} of {expected} requested "
            f"sample_id(s) were not found in the Hugging Face dataset: {missing}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize the human-curated demo fixture.")
    parser.add_argument(
        "--triage-csv",
        action="append",
        default=None,
        help="Triage CSV path. Repeatable: pass it multiple times to merge "
        "several CSVs into one de-duplicated union fixture "
        "(e.g. --triage-csv data/triage_notes.csv "
        "--triage-csv data/hitl_candidate_notes.csv). A sample_id appearing "
        f"in more than one CSV is an error. Defaults to {DEFAULT_TRIAGE_CSV}.",
    )
    parser.add_argument("--sample-root", default=DEFAULT_SAMPLE_ROOT)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Upper bound on HF rows scanned. 0 means scan until all IDs are found.",
    )
    parser.add_argument("--skip-materialize", action="store_true")
    parser.add_argument("--skip-gallery", action="store_true")
    parser.add_argument("--overwrite", action="store_true", default=True)
    parser.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    python = sys.executable
    script_dir = Path(__file__).resolve().parent

    triage_csvs = [Path(p) for p in (args.triage_csv or [DEFAULT_TRIAGE_CSV])]
    sample_root = Path(args.sample_root)
    sample_id_file = sample_root / "demo_sample_ids.txt"
    status_overlay_file = sample_root / "run_status_overlay.json"
    hf_dataset_dir = sample_root / "hf_dataset"
    parquet_path = sample_root / "molmoweb_humanskills_sample.parquet"
    manifest_path = sample_root / "materialize_manifest.json"
    gallery_dir = sample_root / "image_gallery"

    rows = read_triage_csvs(triage_csvs)
    joined = ", ".join(str(p) for p in triage_csvs)
    if not rows:
        print(f"No samples in {joined}", file=sys.stderr)
        return 1

    print(f"Loaded {len(rows)} curated samples from {joined}")
    print(category_summary(rows))

    if not args.dry_run:
        write_sample_ids(rows, sample_id_file)
        write_status_overlay(rows, status_overlay_file)
    print(f"Wrote sample IDs    → {sample_id_file}")
    print(f"Wrote status overlay → {status_overlay_file}")

    if not args.skip_materialize:
        cmd = [
            python,
            str(script_dir / "materialize_molmoweb_sample.py"),
            "--sample-id-file", str(sample_id_file),
            "--max-rows", str(args.max_rows),
            "--output-dir", str(hf_dataset_dir),
            "--parquet-path", str(parquet_path),
            "--manifest-path", str(manifest_path),
        ]
        if args.overwrite:
            cmd.append("--overwrite")
        run_subprocess(cmd, args.dry_run)
        if not args.dry_run:
            assert_materialization_complete(manifest_path, expected=len(rows))

    if not args.skip_gallery:
        cmd = [
            python,
            str(script_dir / "export_molmoweb_images.py"),
            "--input-dir", str(hf_dataset_dir),
            "--output-dir", str(gallery_dir),
        ]
        if args.overwrite:
            cmd.append("--overwrite")
        run_subprocess(cmd, args.dry_run)

    print("\nDone.")
    print(f"  Dataset:  {hf_dataset_dir}")
    print(f"  Gallery:  {gallery_dir / 'index.html'}")
    print(f"  Overlay:  {status_overlay_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
