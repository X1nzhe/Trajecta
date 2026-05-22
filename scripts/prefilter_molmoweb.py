#!/usr/bin/env python3
"""Prefilter MolmoWeb-HumanSkills trajectories with Hugging Face streaming.

The script intentionally performs a cheap metadata pass. It avoids materializing
image bytes by selecting only the columns needed to decide whether a trajectory
is worth human review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


DATASET_NAME = "allenai/MolmoWeb-HumanSkills"
DEFAULT_OUTPUT_DIR = "data/raw/molmoweb_humanskills_candidates"
METADATA_COLUMNS = ["sample_id", "instruction", "trajectory", "image_paths"]
VALID_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
PAREN_COORD_RE = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\)")
XY_COORD_RE = re.compile(
    r"\bx\s*[:=]\s*-?\d+(?:\.\d+)?.{0,24}\by\s*[:=]\s*-?\d+(?:\.\d+)?",
    re.IGNORECASE,
)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    if load_dotenv is not None:
        load_dotenv(path, override=False)
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def load_jsonish(value: Any, field_name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name}_invalid_json") from exc
    return value


def compact_json(value: Any, max_chars: int = 700) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def extract_task(instruction: Any) -> str:
    try:
        parsed = load_jsonish(instruction, "instruction")
    except ValueError:
        return str(instruction)[:700]

    if isinstance(parsed, dict):
        for key in ("low_level", "task", "instruction", "goal", "high_level"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return compact_json(parsed)

    return str(parsed)[:700]


def numeric_step_items(trajectory: dict[str, Any]) -> list[tuple[int, Any]]:
    items: list[tuple[int, Any]] = []
    for key, value in trajectory.items():
        try:
            items.append((int(key), value))
        except (TypeError, ValueError):
            raise ValueError("trajectory_has_non_numeric_step_key") from None
    return sorted(items, key=lambda item: item[0])


def as_action_text(raw_action: Any) -> str:
    if raw_action is None:
        return ""
    if isinstance(raw_action, str):
        return raw_action
    if not isinstance(raw_action, dict):
        return str(raw_action)

    parts: list[str] = []
    for key in ("action_str", "action_description", "action_name"):
        value = raw_action.get(key)
        if value:
            parts.append(str(value))

    output = raw_action.get("action_output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            parts.append(output)
            output = None
    if isinstance(output, dict):
        for key in ("action_name", "name", "type"):
            value = output.get(key)
            if value:
                parts.append(str(value))

        for key in ("parameters", "params", "args", "action_parameters", "action_params"):
            value = output.get(key)
            if value:
                parts.append(compact_json(value, max_chars=300))

    return " ".join(parts)


def normalize_action_type(raw_action: Any) -> str:
    text = as_action_text(raw_action).lower().replace("-", "_").replace(" ", "_")

    if any(token in text for token in ("click", "tap", "press")):
        return "click"
    if any(token in text for token in ("type", "input", "enter_text", "fill", "write")):
        return "type"
    if any(token in text for token in ("scroll", "wheel")):
        return "scroll"
    if any(token in text for token in ("navigate", "go_to", "goto", "open_url", "visit", "url")):
        return "navigate"
    if "wait" in text or "sleep" in text:
        return "wait"
    return "unknown"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def has_coordinate(value: Any) -> bool:
    if isinstance(value, str):
        return bool(PAREN_COORD_RE.search(value) or XY_COORD_RE.search(value))

    if isinstance(value, dict):
        if is_number(value.get("x")) and is_number(value.get("y")):
            return True
        if is_number(value.get("left")) and is_number(value.get("top")):
            return True
        return any(has_coordinate(child) for child in value.values())

    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and is_number(value[0]) and is_number(value[1]):
            return True
        return any(has_coordinate(child) for child in value)

    return False


def unique_limited(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def score_candidate(
    step_count: int,
    screenshot_coverage: float | None,
    action_counts: Counter[str],
    coordinate_action_count: int,
    missing_image_paths_score: float,
    prefer_unknown_actions: bool,
) -> float:
    coverage_score = screenshot_coverage if screenshot_coverage is not None else missing_image_paths_score
    unknown_action_ratio = action_counts["unknown"] / step_count if step_count else 0.0
    known_actions = step_count - action_counts["unknown"]
    known_action_ratio = known_actions / step_count if step_count else 0.0
    action_ratio_score = unknown_action_ratio if prefer_unknown_actions else known_action_ratio
    action_diversity = len([name for name, count in action_counts.items() if name != "unknown" and count])
    moderate_length_bonus = 1.0 if 5 <= step_count <= 20 else 0.5
    coordinate_bonus = min(coordinate_action_count, 5) / 5

    return round(
        45 * coverage_score
        + 30 * action_ratio_score
        + 10 * min(action_diversity, 4) / 4
        + 10 * moderate_length_bonus
        + 5 * coordinate_bonus,
        3,
    )


def evaluate_row(row: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    sample_id = str(row.get("sample_id") or "")
    if not VALID_RUN_ID_RE.fullmatch(sample_id):
        return None, "bad_sample_id"

    try:
        trajectory = load_jsonish(row.get("trajectory"), "trajectory")
    except ValueError as exc:
        return None, str(exc)

    if not isinstance(trajectory, dict):
        return None, "trajectory_not_object"

    try:
        steps = numeric_step_items(trajectory)
    except ValueError as exc:
        return None, str(exc)

    step_count = len(steps)
    if step_count < args.min_steps:
        return None, "too_few_steps"
    if step_count > args.max_steps:
        return None, "too_many_steps"

    raw_image_paths = row.get("image_paths")
    if isinstance(raw_image_paths, list) and raw_image_paths:
        image_path_status = "available"
        image_paths = {str(path) for path in raw_image_paths}
    elif isinstance(raw_image_paths, list):
        image_path_status = "empty"
        image_paths = set()
    elif raw_image_paths is None:
        image_path_status = "missing"
        image_paths = set()
    else:
        image_path_status = "invalid"
        image_paths = set()

    if args.require_image_paths and image_path_status != "available":
        return None, f"image_paths_{image_path_status}"

    screenshot_refs: list[str] = []
    missing_screenshots: list[str] = []
    action_counts: Counter[str] = Counter()
    coordinate_action_count = 0
    urls: list[str] = []

    for _, step in steps:
        if not isinstance(step, dict):
            return None, "step_not_object"

        screenshot = step.get("screenshot")
        if screenshot:
            screenshot_name = str(screenshot)
            screenshot_refs.append(screenshot_name)
            if image_path_status == "available" and screenshot_name not in image_paths:
                missing_screenshots.append(screenshot_name)

        raw_action = step.get("action")
        action_type = normalize_action_type(raw_action)
        action_counts[action_type] += 1
        if has_coordinate(raw_action):
            coordinate_action_count += 1

        other_obs = step.get("other_obs") or {}
        if isinstance(other_obs, dict):
            url = other_obs.get("current_url") or other_obs.get("url")
            if isinstance(url, str) and url:
                urls.append(url)

    if not screenshot_refs:
        return None, "no_screenshot_refs"

    screenshot_coverage = None
    matched_screenshot_refs = None
    if image_path_status == "available":
        matched_screenshot_refs = len(screenshot_refs) - len(missing_screenshots)
        screenshot_coverage = matched_screenshot_refs / len(screenshot_refs)

    if (
        args.require_image_path_match
        and screenshot_coverage is not None
        and screenshot_coverage < args.min_screenshot_coverage
    ):
        return None, "low_screenshot_coverage"

    unknown_action_ratio = action_counts["unknown"] / step_count if step_count else 1.0
    if args.min_unknown_action_ratio > 0 and unknown_action_ratio <= args.min_unknown_action_ratio:
        return None, "too_few_unknown_actions"
    if unknown_action_ratio > args.max_unknown_action_ratio:
        return None, "too_many_unknown_actions"

    task = extract_task(row.get("instruction"))
    score = score_candidate(
        step_count,
        screenshot_coverage,
        action_counts,
        coordinate_action_count,
        args.missing_image_paths_score,
        args.prefer_unknown_actions,
    )

    candidate = {
        "sample_id": sample_id,
        "task": task,
        "step_count": step_count,
        "score": score,
        "screenshot_refs": len(screenshot_refs),
        "image_path_status": image_path_status,
        "matched_screenshot_refs": matched_screenshot_refs,
        "screenshot_coverage": round(screenshot_coverage, 3) if screenshot_coverage is not None else None,
        "missing_screenshot_examples": unique_limited(missing_screenshots, 5),
        "action_type_counts": dict(sorted(action_counts.items())),
        "unknown_action_ratio": round(unknown_action_ratio, 3),
        "coordinate_action_count": coordinate_action_count,
        "url_examples": unique_limited(urls, 5),
    }
    return candidate, "accepted"


def load_stream(args: argparse.Namespace) -> Any:
    try:
        from datasets import load_dataset
    except ImportError:
        print(
            "Missing dependency: install Hugging Face datasets first, e.g. "
            "`pip install datasets`.",
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

    dataset = load_dataset(**load_kwargs)
    if args.metadata_only:
        try:
            dataset = dataset.select_columns(METADATA_COLUMNS)
        except Exception as exc:  # pragma: no cover - depends on datasets version.
            print(
                f"Warning: could not select metadata-only columns ({exc}); "
                "streaming will still avoid full materialization.",
                file=sys.stderr,
            )
    return dataset


def write_outputs(
    output_dir: Path,
    candidates: list[dict[str, Any]],
    all_candidate_count: int,
    scanned_count: int,
    rejected_counts: Counter[str],
    args: argparse.Namespace,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates_path = output_dir / "candidates.jsonl"
    with candidates_path.open("w", encoding="utf-8") as file:
        for rank, candidate in enumerate(candidates, start=1):
            ranked = {"rank": rank, **candidate}
            file.write(json.dumps(ranked, ensure_ascii=False, sort_keys=True) + "\n")

    selected_ids_path = output_dir / "candidate_sample_ids.txt"
    with selected_ids_path.open("w", encoding="utf-8") as file:
        for candidate in candidates:
            file.write(candidate["sample_id"] + "\n")

    summary = {
        "dataset": args.dataset,
        "split": args.split,
        "scanned_rows": scanned_count,
        "accepted_before_ranking": all_candidate_count,
        "written_candidates": len(candidates),
        "rejected_counts": dict(sorted(rejected_counts.items())),
        "settings": {
            "max_rows": args.max_rows,
            "target_candidates": args.target_candidates,
            "min_steps": args.min_steps,
            "max_steps": args.max_steps,
            "min_screenshot_coverage": args.min_screenshot_coverage,
            "missing_image_paths_score": args.missing_image_paths_score,
            "min_unknown_action_ratio": args.min_unknown_action_ratio,
            "max_unknown_action_ratio": args.max_unknown_action_ratio,
            "prefer_unknown_actions": args.prefer_unknown_actions,
            "metadata_only": args.metadata_only,
            "require_image_paths": args.require_image_paths,
            "require_image_path_match": args.require_image_path_match,
            "hf_token_env": args.hf_token_env,
            "hf_token_used": bool(args.hf_token or os.environ.get(args.hf_token_env) or args.use_cached_token),
        },
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Wrote {len(candidates)} candidates to {candidates_path}")
    print(f"Wrote sample IDs to {selected_ids_path}")
    print(f"Wrote summary to {summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream MolmoWeb-HumanSkills and write candidate trajectories for human review."
    )
    parser.add_argument("--dataset", default=DATASET_NAME)
    parser.add_argument("--split", default="train")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
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
    parser.add_argument("--max-rows", type=int, default=2000, help="Maximum streamed rows to inspect.")
    parser.add_argument("--target-candidates", type=int, default=50, help="Top candidates to write after ranking.")
    parser.add_argument("--min-steps", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--min-screenshot-coverage", type=float, default=1.0)
    parser.add_argument(
        "--missing-image-paths-score",
        type=float,
        default=0.75,
        help="Score contribution to use when image_paths is missing and screenshot coverage cannot be measured.",
    )
    parser.add_argument(
        "--min-unknown-action-ratio",
        type=float,
        default=0.0,
        help="Exclusive lower bound for unknown action ratio. Useful for anomaly-only pools.",
    )
    parser.add_argument("--max-unknown-action-ratio", type=float, default=0.4)
    parser.add_argument(
        "--prefer-unknown-actions",
        action="store_true",
        help="Rank candidates with more unknown actions higher. Useful for anomaly pools.",
    )
    parser.add_argument(
        "--require-image-paths",
        action="store_true",
        help="Reject rows whose image_paths field is missing, empty, or invalid.",
    )
    parser.add_argument(
        "--require-image-path-match",
        action="store_true",
        help="Reject rows when present image_paths do not match trajectory screenshot references.",
    )
    parser.add_argument(
        "--metadata-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Select only sample_id, instruction, trajectory, and image_paths while screening.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    if args.env_file:
        load_env_file(Path(args.env_file))

    dataset = load_stream(args)
    rejected_counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    scanned_count = 0

    for row in dataset:
        if args.max_rows and scanned_count >= args.max_rows:
            break
        scanned_count += 1

        candidate, reason = evaluate_row(row, args)
        if candidate is None:
            rejected_counts[reason] += 1
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda item: (-item["score"], item["step_count"], item["sample_id"]))
    selected = candidates[: args.target_candidates]

    write_outputs(
        output_dir=output_dir,
        candidates=selected,
        all_candidate_count=len(candidates),
        scanned_count=scanned_count,
        rejected_counts=rejected_counts,
        args=args,
    )

    print(f"Scanned rows: {scanned_count}")
    print(f"Accepted before ranking: {len(candidates)}")
    print(f"Rejected counts: {dict(sorted(rejected_counts.items()))}")
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
