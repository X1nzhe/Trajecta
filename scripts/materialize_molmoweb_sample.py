#!/usr/bin/env python3
"""Materialize selected MolmoWeb-HumanSkills rows by sample_id.

Hugging Face Hub search does not index dataset row values such as sample_id.
This script streams the dataset, filters rows locally, and saves matching rows
with the same row fields as the source dataset.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from prefilter_molmoweb import DATASET_NAME, load_env_file
except ImportError:
    from scripts.prefilter_molmoweb import DATASET_NAME, load_env_file


DEFAULT_SAMPLE_ID_FILE = "data/raw/molmoweb_humanskills_sample/_work/candidate_sample_ids.txt"
DEFAULT_OUTPUT_DIR = "data/raw/molmoweb_humanskills_sample/hf_dataset"
DEFAULT_PARQUET_PATH = "data/raw/molmoweb_humanskills_sample/molmoweb_humanskills_sample.parquet"
DEFAULT_MANIFEST_PATH = "data/raw/molmoweb_humanskills_sample/materialize_manifest.json"


def read_sample_ids(path: Path) -> list[str]:
    sample_ids: list[str] = []
    seen: set[str] = set()
    if not path.exists():
        raise FileNotFoundError(f"sample id file does not exist: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        sample_id = line.split()[0]
        if sample_id in seen:
            continue
        seen.add(sample_id)
        sample_ids.append(sample_id)
    return sample_ids


def read_sample_ids_many(paths: list[Path]) -> list[str]:
    sample_ids: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for sample_id in read_sample_ids(path):
            if sample_id in seen:
                continue
            seen.add(sample_id)
            sample_ids.append(sample_id)
    return sample_ids


def load_stream(args: argparse.Namespace) -> Any:
    try:
        from datasets import load_dataset
    except ImportError:
        print(
            "Missing dependency: install Hugging Face datasets first, e.g. "
            "`pip install datasets python-dotenv`.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    load_kwargs = {
        "path": args.dataset,
        "split": args.split,
        "streaming": True,
    }
    hf_token = args.hf_token or os.environ.get(args.hf_token_env)
    if hf_token:
        load_kwargs["token"] = hf_token
    elif args.use_cached_token:
        load_kwargs["token"] = True

    return load_dataset(**load_kwargs)


def collect_matching_rows(dataset: Any, sample_ids: list[str], max_rows: int) -> tuple[list[dict[str, Any]], int]:
    wanted = set(sample_ids)
    rows_by_id: dict[str, dict[str, Any]] = {}
    scanned_count = 0

    for row in dataset:
        if max_rows and scanned_count >= max_rows:
            break
        scanned_count += 1

        sample_id = str(row.get("sample_id") or "")
        if sample_id not in wanted or sample_id in rows_by_id:
            continue

        rows_by_id[sample_id] = dict(row)
        print(f"Matched {len(rows_by_id)}/{len(wanted)}: {sample_id}")
        if len(rows_by_id) == len(wanted):
            break

    rows = [rows_by_id[sample_id] for sample_id in sample_ids if sample_id in rows_by_id]
    return rows, scanned_count


def build_hf_dataset(rows: list[dict[str, Any]]) -> Any:
    try:
        from datasets import Dataset
    except ImportError:
        print(
            "Missing dependency: install Hugging Face datasets first, e.g. "
            "`pip install datasets python-dotenv`.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return Dataset.from_list(rows)


def save_hf_dataset(dataset: Any, output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}; pass --overwrite to replace it")
        shutil.rmtree(output_dir)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_to_disk(str(output_dir))


def save_parquet(dataset: Any, parquet_path: Path, overwrite: bool) -> None:
    if parquet_path.exists():
        if not overwrite:
            raise FileExistsError(f"parquet file already exists: {parquet_path}; pass --overwrite to replace it")
        parquet_path.unlink()

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_parquet(str(parquet_path))


def write_manifest(
    manifest_path: Path,
    args: argparse.Namespace,
    requested_sample_ids: list[str],
    found_rows: list[dict[str, Any]],
    scanned_count: int,
) -> None:
    found_sample_ids = [str(row.get("sample_id") or "") for row in found_rows]
    missing_sample_ids = [sample_id for sample_id in requested_sample_ids if sample_id not in set(found_sample_ids)]
    manifest = {
        "dataset": args.dataset,
        "split": args.split,
        "sample_id_files": args.sample_id_file,
        "output_dir": args.output_dir,
        "parquet_path": args.parquet_path,
        "scanned_rows": scanned_count,
        "requested_count": len(requested_sample_ids),
        "found_count": len(found_sample_ids),
        "missing_count": len(missing_sample_ids),
        "found_sample_ids": found_sample_ids,
        "missing_sample_ids": missing_sample_ids,
        "max_rows": args.max_rows,
        "hf_token_env": args.hf_token_env,
        "hf_token_used": bool(args.hf_token or os.environ.get(args.hf_token_env) or args.use_cached_token),
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream MolmoWeb-HumanSkills and save selected sample_id rows as a local HF Dataset."
    )
    parser.add_argument("--dataset", default=DATASET_NAME)
    parser.add_argument("--split", default="train")
    parser.add_argument("--sample-id-file", action="append", default=None)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--parquet-path",
        default=DEFAULT_PARQUET_PATH,
        help="Optional local parquet output. Pass an empty string to skip parquet export.",
    )
    parser.add_argument("--manifest-path", default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional dotenv-style file to load before reading HF_TOKEN. Existing environment values win.",
    )
    parser.add_argument(
        "--hf-token-env",
        default="HF_TOKEN",
        help="Environment variable containing a Hugging Face token for higher rate limits.",
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        help="Hugging Face token. Prefer --hf-token-env or `huggingface-cli login` for shell history safety.",
    )
    parser.add_argument(
        "--use-cached-token",
        action="store_true",
        help="Pass token=True to use a token saved by `huggingface-cli login`.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Maximum streamed rows to inspect. Use 0 for no explicit limit.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output dataset directory.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.env_file:
        load_env_file(Path(args.env_file))

    sample_id_files = args.sample_id_file or [DEFAULT_SAMPLE_ID_FILE]
    args.sample_id_file = sample_id_files
    requested_sample_ids = read_sample_ids_many([Path(path) for path in sample_id_files])
    if not requested_sample_ids:
        print(f"No sample IDs found in {args.sample_id_file}", file=sys.stderr)
        return 1

    dataset = load_stream(args)
    rows, scanned_count = collect_matching_rows(dataset, requested_sample_ids, args.max_rows)
    if not rows:
        print("No requested sample IDs were found. Increase --max-rows or verify the id file.", file=sys.stderr)
        write_manifest(Path(args.manifest_path), args, requested_sample_ids, rows, scanned_count)
        return 1

    dataset = build_hf_dataset(rows)
    save_hf_dataset(dataset, Path(args.output_dir), args.overwrite)
    if args.parquet_path:
        save_parquet(dataset, Path(args.parquet_path), args.overwrite)
    write_manifest(Path(args.manifest_path), args, requested_sample_ids, rows, scanned_count)

    missing_count = len(requested_sample_ids) - len(rows)
    print(f"Wrote {len(rows)} rows to {args.output_dir}")
    if args.parquet_path:
        print(f"Wrote parquet to {args.parquet_path}")
    print(f"Wrote manifest to {args.manifest_path}")
    if missing_count:
        print(f"Missing {missing_count} requested sample IDs; see manifest for details.", file=sys.stderr)
        return 1
    return 0


def exit_process(code: int) -> None:
    # HF streaming can leave background workers alive after the useful work is done.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    exit_process(main())
