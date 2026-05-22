#!/usr/bin/env python3
"""Run the MolmoWeb sample preparation pipeline end to end."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_MAX_ROWS = 1000
DEFAULT_TARGET_CANDIDATES = 100
DEFAULT_MIN_STEPS = 3
DEFAULT_MAX_STEPS = 60
DEFAULT_CLEAN_UNKNOWN_RATIO = 0.4
DEFAULT_ANOMALY_UNKNOWN_RATIO = 1.0
DEFAULT_BASE_CANDIDATE_DIR = "data/raw/molmoweb_humanskills_candidates"
DEFAULT_SAMPLE_ROOT = "data/raw/molmoweb_humanskills_sample"


def run_command(cmd: list[str], dry_run: bool) -> None:
    print("\n$ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def merge_sample_ids(clean_file: Path, anomaly_file: Path, output_file: Path, limit: int) -> None:
    sample_ids: list[str] = []
    seen: set[str] = set()
    clean_ids = set(read_sample_ids(clean_file))
    anomaly_ids = set(read_sample_ids(anomaly_file))
    overlap = clean_ids & anomaly_ids
    if overlap:
        examples = ", ".join(sorted(overlap)[:5])
        print(f"Warning: clean/anomaly overlap contains {len(overlap)} sample IDs: {examples}", file=sys.stderr)

    for path in (clean_file, anomaly_file):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            sample_id = raw_line.strip()
            if not sample_id or sample_id.startswith("#") or sample_id in seen:
                continue
            seen.add(sample_id)
            sample_ids.append(sample_id)
            if limit and len(sample_ids) >= limit:
                break
        if limit and len(sample_ids) >= limit:
            break

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(sample_ids) + ("\n" if sample_ids else ""), encoding="utf-8")
    print(f"Merged {len(sample_ids)} sample IDs into {output_file}")


def read_sample_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    sample_ids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        sample_id = raw_line.strip()
        if sample_id and not sample_id.startswith("#"):
            sample_ids.append(sample_id)
    return sample_ids


def has_sample_ids(path: Path) -> bool:
    return bool(read_sample_ids(path))


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run prefilter, materialize, and gallery export for MolmoWeb.")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--target-candidates", type=int, default=DEFAULT_TARGET_CANDIDATES)
    parser.add_argument("--min-steps", type=int, default=DEFAULT_MIN_STEPS)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--clean-unknown-ratio", type=float, default=DEFAULT_CLEAN_UNKNOWN_RATIO)
    parser.add_argument("--anomaly-unknown-ratio", type=float, default=DEFAULT_ANOMALY_UNKNOWN_RATIO)
    parser.add_argument(
        "--materialize-limit",
        type=int,
        default=0,
        help="Limit merged IDs before materialization. Use 0 to materialize all merged candidates.",
    )
    parser.add_argument("--candidate-dir", default=DEFAULT_BASE_CANDIDATE_DIR)
    parser.add_argument("--sample-root", default=DEFAULT_SAMPLE_ROOT)
    parser.add_argument("--skip-clean", action="store_true")
    parser.add_argument("--skip-anomaly", action="store_true")
    parser.add_argument("--skip-materialize", action="store_true")
    parser.add_argument("--skip-gallery", action="store_true")
    parser.add_argument("--overwrite", action="store_true", default=True)
    parser.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.clean_unknown_ratio >= args.anomaly_unknown_ratio:
        raise ValueError("--clean-unknown-ratio must be lower than --anomaly-unknown-ratio")

    python = sys.executable
    script_dir = Path(__file__).resolve().parent

    candidate_dir = Path(args.candidate_dir)
    clean_dir = candidate_dir / "clean"
    anomaly_dir = candidate_dir / "anomaly"
    merged_id_file = candidate_dir / "selected_sample_ids.txt"
    sample_root = Path(args.sample_root)
    hf_dataset_dir = sample_root / "hf_dataset"
    parquet_path = sample_root / "molmoweb_humanskills_sample.parquet"
    manifest_path = sample_root / "materialize_manifest.json"
    gallery_dir = sample_root / "image_gallery"
    pools_root = sample_root / "pools"
    clean_pool_root = pools_root / "clean"
    anomaly_pool_root = pools_root / "anomaly"
    clean_hf_dataset_dir = clean_pool_root / "hf_dataset"
    anomaly_hf_dataset_dir = anomaly_pool_root / "hf_dataset"
    clean_gallery_dir = clean_pool_root / "image_gallery"
    anomaly_gallery_dir = anomaly_pool_root / "image_gallery"
    clean_id_file = clean_dir / "candidate_sample_ids.txt"
    anomaly_id_file = anomaly_dir / "candidate_sample_ids.txt"

    if not args.skip_clean:
        run_command(
            [
                python,
                str(script_dir / "prefilter_molmoweb.py"),
                "--max-rows",
                str(args.max_rows),
                "--target-candidates",
                str(args.target_candidates),
                "--min-steps",
                str(args.min_steps),
                "--max-steps",
                str(args.max_steps),
                "--max-unknown-action-ratio",
                str(args.clean_unknown_ratio),
                "--output-dir",
                str(clean_dir),
            ],
            args.dry_run,
        )

    if not args.skip_anomaly:
        run_command(
            [
                python,
                str(script_dir / "prefilter_molmoweb.py"),
                "--max-rows",
                str(args.max_rows),
                "--target-candidates",
                str(args.target_candidates),
                "--min-steps",
                str(args.min_steps),
                "--max-steps",
                str(args.max_steps),
                "--max-unknown-action-ratio",
                str(args.anomaly_unknown_ratio),
                "--min-unknown-action-ratio",
                str(args.clean_unknown_ratio),
                "--prefer-unknown-actions",
                "--output-dir",
                str(anomaly_dir),
            ],
            args.dry_run,
        )

    if args.dry_run:
        print(f"\nWould merge sample IDs into {merged_id_file}")
    else:
        merge_sample_ids(
            clean_file=clean_id_file,
            anomaly_file=anomaly_id_file,
            output_file=merged_id_file,
            limit=args.materialize_limit,
        )

    if not args.skip_materialize:
        if args.dry_run or has_sample_ids(clean_id_file):
            run_materialize(
                python=python,
                script_dir=script_dir,
                sample_id_file=clean_id_file,
                max_rows=args.max_rows,
                output_dir=clean_hf_dataset_dir,
                parquet_path=clean_pool_root / "molmoweb_humanskills_sample.parquet",
                manifest_path=clean_pool_root / "materialize_manifest.json",
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping clean materialize; no sample IDs in {clean_id_file}")
        if args.dry_run or has_sample_ids(anomaly_id_file):
            run_materialize(
                python=python,
                script_dir=script_dir,
                sample_id_file=anomaly_id_file,
                max_rows=args.max_rows,
                output_dir=anomaly_hf_dataset_dir,
                parquet_path=anomaly_pool_root / "molmoweb_humanskills_sample.parquet",
                manifest_path=anomaly_pool_root / "materialize_manifest.json",
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping anomaly materialize; no sample IDs in {anomaly_id_file}")
        run_materialize(
            python=python,
            script_dir=script_dir,
            sample_id_file=merged_id_file,
            max_rows=args.max_rows,
            output_dir=hf_dataset_dir,
            parquet_path=parquet_path,
            manifest_path=manifest_path,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )

    if not args.skip_gallery:
        if args.dry_run or clean_hf_dataset_dir.exists():
            run_gallery(
                python=python,
                script_dir=script_dir,
                input_dir=clean_hf_dataset_dir,
                output_dir=clean_gallery_dir,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping clean gallery; dataset does not exist at {clean_hf_dataset_dir}")
        if args.dry_run or anomaly_hf_dataset_dir.exists():
            run_gallery(
                python=python,
                script_dir=script_dir,
                input_dir=anomaly_hf_dataset_dir,
                output_dir=anomaly_gallery_dir,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        else:
            print(f"Skipping anomaly gallery; dataset does not exist at {anomaly_hf_dataset_dir}")
        run_gallery(
            python=python,
            script_dir=script_dir,
            input_dir=hf_dataset_dir,
            output_dir=gallery_dir,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )

    print("\nPipeline complete.")
    print(f"Clean candidates: {clean_dir / 'candidates.jsonl'}")
    print(f"Anomaly candidates: {anomaly_dir / 'candidates.jsonl'}")
    print(f"Selected IDs: {merged_id_file}")
    print(f"Clean dataset: {clean_hf_dataset_dir}")
    print(f"Anomaly dataset: {anomaly_hf_dataset_dir}")
    print(f"Materialized dataset: {hf_dataset_dir}")
    print(f"Clean gallery: {clean_gallery_dir / 'index.html'}")
    print(f"Anomaly gallery: {anomaly_gallery_dir / 'index.html'}")
    print(f"Gallery: {gallery_dir / 'index.html'}")
    return 0


def exit_process(code: int) -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    exit_process(main())
