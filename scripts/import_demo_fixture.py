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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize the human-curated demo fixture.")
    parser.add_argument("--triage-csv", default=DEFAULT_TRIAGE_CSV)
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

    triage_csv = Path(args.triage_csv)
    sample_root = Path(args.sample_root)
    sample_id_file = sample_root / "demo_sample_ids.txt"
    status_overlay_file = sample_root / "run_status_overlay.json"
    hf_dataset_dir = sample_root / "hf_dataset"
    parquet_path = sample_root / "molmoweb_humanskills_sample.parquet"
    manifest_path = sample_root / "materialize_manifest.json"
    gallery_dir = sample_root / "image_gallery"

    rows = read_triage_csv(triage_csv)
    if not rows:
        print(f"No samples in {triage_csv}", file=sys.stderr)
        return 1

    print(f"Loaded {len(rows)} curated samples from {triage_csv}")
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
