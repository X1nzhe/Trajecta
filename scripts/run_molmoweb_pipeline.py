#!/usr/bin/env python3
"""Run the MolmoWeb sample preparation pipeline end to end.

Stages:
  1. Stream metadata once and rank candidate sample IDs (prefilter).
  2. Stream the dataset again and materialize candidate rows with images.
  3. Export a candidate image gallery (also yields the quality manifest).
  4. Drop samples flagged by the gallery quality manifest.
  5. Subset the local candidate dataset down to the quality-passing IDs.
  6. Export the final selected image gallery.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from materialize_molmoweb_sample import (
        read_sample_ids,
        save_hf_dataset,
        save_parquet,
    )
except ImportError:
    from scripts.materialize_molmoweb_sample import (
        read_sample_ids,
        save_hf_dataset,
        save_parquet,
    )


DEFAULT_MAX_ROWS = 1000
DEFAULT_TARGET_CANDIDATES = 100
DEFAULT_MIN_STEPS = 3
DEFAULT_MAX_STEPS = 60
DEFAULT_MAX_UNKNOWN_RATIO = 0.4
DEFAULT_REJECT_QUALITY_FLAGS = (
    "low_bytes",
    "small_dimensions",
    "low_pixel_count",
    "unknown_dimensions",
)
DEFAULT_SAMPLE_ROOT = "data/raw/molmoweb_humanskills_sample"


def run_command(cmd: list[str], dry_run: bool) -> None:
    print("\n$ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def run_prefilter(
    *,
    python: str,
    script_dir: Path,
    output_dir: Path,
    max_rows: int,
    target_candidates: int,
    min_steps: int,
    max_steps: int,
    max_unknown_ratio: float,
    dry_run: bool,
) -> None:
    cmd = [
        python,
        str(script_dir / "prefilter_molmoweb.py"),
        "--output-dir",
        str(output_dir),
        "--max-rows",
        str(max_rows),
        "--target-candidates",
        str(target_candidates),
        "--min-steps",
        str(min_steps),
        "--max-steps",
        str(max_steps),
        "--max-unknown-action-ratio",
        str(max_unknown_ratio),
    ]
    run_command(cmd, dry_run)


def run_materialize(
    *,
    python: str,
    script_dir: Path,
    sample_id_file: Path,
    max_rows: int,
    output_dir: Path,
    parquet_path: Path,
    manifest_path: Path,
    overwrite: bool,
    dry_run: bool,
) -> None:
    cmd = [
        python,
        str(script_dir / "materialize_molmoweb_sample.py"),
        "--sample-id-file",
        str(sample_id_file),
        "--max-rows",
        str(max_rows),
        "--output-dir",
        str(output_dir),
        "--parquet-path",
        str(parquet_path),
        "--manifest-path",
        str(manifest_path),
    ]
    if overwrite:
        cmd.append("--overwrite")
    run_command(cmd, dry_run)


def run_gallery(
    *,
    python: str,
    script_dir: Path,
    input_dir: Path,
    output_dir: Path,
    overwrite: bool,
    dry_run: bool,
) -> None:
    if output_dir.exists() and overwrite and not dry_run:
        shutil.rmtree(output_dir)
    cmd = [
        python,
        str(script_dir / "export_molmoweb_images.py"),
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(output_dir),
    ]
    if overwrite:
        cmd.append("--overwrite")
    run_command(cmd, dry_run)


def filter_by_quality(
    *,
    candidate_id_file: Path,
    manifest_path: Path,
    selected_id_file: Path,
    rejected_id_file: Path,
    summary_file: Path,
    reject_flags: tuple[str, ...],
) -> int:
    sample_ids = read_sample_ids(candidate_id_file)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = manifest.get("samples") or {}
    reject_set = set(reject_flags)

    kept: list[str] = []
    rejected: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        sample = samples.get(sample_id)
        if not sample:
            rejected.append({"sample_id": sample_id, "reason": "missing_gallery_manifest_sample"})
            continue
        matched = sorted(set(sample.get("quality_flags") or []) & reject_set)
        if matched:
            rejected.append(
                {
                    "sample_id": sample_id,
                    "reason": "rejected_quality_flags",
                    "matched_flags": matched,
                    "image_count": sample.get("image_count", 0),
                    "flagged_image_count": sample.get("flagged_image_count", 0),
                }
            )
        else:
            kept.append(sample_id)

    selected_id_file.parent.mkdir(parents=True, exist_ok=True)
    selected_id_file.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    rejected_id_file.write_text(
        "\n".join(item["sample_id"] for item in rejected) + ("\n" if rejected else ""),
        encoding="utf-8",
    )
    summary = {
        "candidate_id_file": str(candidate_id_file),
        "manifest_path": str(manifest_path),
        "selected_id_file": str(selected_id_file),
        "rejected_id_file": str(rejected_id_file),
        "reject_flags": sorted(reject_set),
        "input_count": len(sample_ids),
        "kept_count": len(kept),
        "rejected_count": len(rejected),
        "rejected": rejected,
    }
    summary_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"Quality filter kept {len(kept)} / {len(sample_ids)} → {selected_id_file}")
    return len(kept)


def subset_local_dataset(
    *,
    input_dir: Path,
    sample_id_file: Path,
    output_dir: Path,
    parquet_path: Path | None,
    manifest_path: Path,
    overwrite: bool,
) -> int:
    try:
        from datasets import load_from_disk
    except ImportError:
        print("Missing dependency: pip install datasets python-dotenv", file=sys.stderr)
        raise SystemExit(2)

    requested = read_sample_ids(sample_id_file)
    if not requested:
        print(f"No sample IDs in {sample_id_file}; skipping subset.")
        return 0

    if output_dir.exists() and overwrite:
        shutil.rmtree(output_dir)

    dataset = load_from_disk(str(input_dir))
    wanted = set(requested)
    index_by_id: dict[str, int] = {}
    for index, row in enumerate(dataset):
        sample_id = str(row.get("sample_id") or "")
        if sample_id in wanted and sample_id not in index_by_id:
            index_by_id[sample_id] = index

    found = [sid for sid in requested if sid in index_by_id]
    missing = [sid for sid in requested if sid not in index_by_id]
    subset = dataset.select([index_by_id[sid] for sid in found])

    save_hf_dataset(subset, output_dir, overwrite=overwrite)
    if parquet_path:
        save_parquet(subset, parquet_path, overwrite=overwrite)

    manifest = {
        "source": "local_subset",
        "input_dir": str(input_dir),
        "sample_id_file": str(sample_id_file),
        "output_dir": str(output_dir),
        "parquet_path": str(parquet_path) if parquet_path else None,
        "requested_count": len(requested),
        "found_count": len(found),
        "missing_count": len(missing),
        "found_sample_ids": found,
        "missing_sample_ids": missing,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"Subset wrote {len(found)} rows to {output_dir} (missing {len(missing)})")
    return len(found)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run prefilter, materialize, and gallery export for MolmoWeb.")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--target-candidates", type=int, default=DEFAULT_TARGET_CANDIDATES)
    parser.add_argument("--min-steps", type=int, default=DEFAULT_MIN_STEPS)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--max-unknown-action-ratio", type=float, default=DEFAULT_MAX_UNKNOWN_RATIO)
    parser.add_argument("--sample-root", default=DEFAULT_SAMPLE_ROOT)
    parser.add_argument("--skip-prefilter", action="store_true")
    parser.add_argument("--skip-materialize", action="store_true")
    parser.add_argument("--skip-gallery", action="store_true")
    parser.add_argument("--skip-quality-filter", action="store_true")
    parser.add_argument("--overwrite", action="store_true", default=True)
    parser.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    python = sys.executable
    script_dir = Path(__file__).resolve().parent

    sample_root = Path(args.sample_root)
    work_dir = sample_root / "_work"
    candidate_pool_root = work_dir / "candidates"

    candidate_id_file = work_dir / "candidate_sample_ids.txt"
    rejected_id_file = work_dir / "rejected_quality_sample_ids.txt"
    quality_summary_file = work_dir / "quality_summary.json"
    candidate_hf_dataset_dir = candidate_pool_root / "hf_dataset"
    candidate_gallery_dir = candidate_pool_root / "image_gallery"
    candidate_gallery_manifest = candidate_gallery_dir / "manifest.json"

    selected_id_file = sample_root / "selected_sample_ids.txt"
    final_hf_dataset_dir = sample_root / "hf_dataset"
    final_parquet_path = sample_root / "molmoweb_humanskills_sample.parquet"
    final_manifest_path = sample_root / "materialize_manifest.json"
    final_gallery_dir = sample_root / "image_gallery"

    # 1. Prefilter
    if not args.skip_prefilter:
        run_prefilter(
            python=python,
            script_dir=script_dir,
            output_dir=work_dir,
            max_rows=args.max_rows,
            target_candidates=args.target_candidates,
            min_steps=args.min_steps,
            max_steps=args.max_steps,
            max_unknown_ratio=args.max_unknown_action_ratio,
            dry_run=args.dry_run,
        )

    # 2. Materialize candidate rows with images
    if not args.skip_materialize:
        if args.dry_run or read_sample_ids(candidate_id_file):
            run_materialize(
                python=python,
                script_dir=script_dir,
                sample_id_file=candidate_id_file,
                max_rows=args.max_rows,
                output_dir=candidate_hf_dataset_dir,
                parquet_path=candidate_pool_root / "molmoweb_humanskills_sample.parquet",
                manifest_path=candidate_pool_root / "materialize_manifest.json",
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping candidate materialize; no sample IDs in {candidate_id_file}")

    # 3. Candidate gallery (also produces quality manifest)
    if not args.skip_gallery:
        if args.dry_run or candidate_hf_dataset_dir.exists():
            run_gallery(
                python=python,
                script_dir=script_dir,
                input_dir=candidate_hf_dataset_dir,
                output_dir=candidate_gallery_dir,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping candidate gallery; dataset does not exist at {candidate_hf_dataset_dir}")

    # 4. Quality filter (inline)
    if not args.skip_quality_filter:
        if args.dry_run:
            print(f"\nWould filter {candidate_id_file} against {candidate_gallery_manifest}")
        elif candidate_id_file.exists() and candidate_gallery_manifest.exists():
            filter_by_quality(
                candidate_id_file=candidate_id_file,
                manifest_path=candidate_gallery_manifest,
                selected_id_file=selected_id_file,
                rejected_id_file=rejected_id_file,
                summary_file=quality_summary_file,
                reject_flags=DEFAULT_REJECT_QUALITY_FLAGS,
            )
        else:
            print(
                f"Skipping quality filter; missing {candidate_id_file} or {candidate_gallery_manifest}"
            )
    elif not args.dry_run:
        # When quality filtering is skipped, fall through with the full candidate list.
        if candidate_id_file.exists():
            selected_id_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate_id_file, selected_id_file)

    # 5. Subset candidate dataset to selected IDs (inline)
    if not args.skip_materialize:
        if args.dry_run:
            print(f"\nWould subset {candidate_hf_dataset_dir} → {final_hf_dataset_dir}")
        elif selected_id_file.exists() and candidate_hf_dataset_dir.exists():
            subset_local_dataset(
                input_dir=candidate_hf_dataset_dir,
                sample_id_file=selected_id_file,
                output_dir=final_hf_dataset_dir,
                parquet_path=final_parquet_path,
                manifest_path=final_manifest_path,
                overwrite=args.overwrite,
            )
        else:
            print(
                f"Skipping selected subset; missing {selected_id_file} or "
                f"{candidate_hf_dataset_dir}"
            )

    # 6. Final gallery
    if not args.skip_gallery:
        if args.dry_run or final_hf_dataset_dir.exists():
            run_gallery(
                python=python,
                script_dir=script_dir,
                input_dir=final_hf_dataset_dir,
                output_dir=final_gallery_dir,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping selected gallery; dataset does not exist at {final_hf_dataset_dir}")

    print("\nPipeline complete.")
    print(f"Candidate IDs:        {candidate_id_file}")
    print(f"Selected IDs:         {selected_id_file}")
    print(f"Candidate dataset:    {candidate_hf_dataset_dir}")
    print(f"Candidate gallery:    {candidate_gallery_dir / 'index.html'}")
    print(f"Final dataset:        {final_hf_dataset_dir}")
    print(f"Final gallery:        {final_gallery_dir / 'index.html'}")
    return 0


def exit_process(code: int) -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    exit_process(main())
