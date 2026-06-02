"""Probe the configured real VLM with one screenshot from the local DB.

Use when get_step_detail starts returning vlm_summary=null in the trace
and you want to see *why* without re-running a whole analyze. Picks the
first screenshot it finds in data/trajecta.db, sends both a low-detail
and a high-detail VLM call, prints whatever each one returns (or the
exception logged on failure).

Usage:
    python scripts/probe_vlm.py
    python scripts/probe_vlm.py --trajectory-id <trajectory_id>     # specific run
    python scripts/probe_vlm.py --filename screenshot_005.png

The script honors .env (auto-loaded) and respects the same TRAJECTA_*
env vars the backend reads. Exits non-zero if either call fails.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

# Enable INFO-level logging on backend.app.llm so the new exception
# loggers we added land in the console.
logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

from backend.app import db, llm, storage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trajectory-id", dest="trajectory_id", default=None)
    parser.add_argument("--filename", default=None)
    args = parser.parse_args()

    db.init_schema()
    runs = storage.list_trajectories()
    if not runs:
        print("no runs in data/trajecta.db — Import Dataset first", file=sys.stderr)
        return 1

    target_trajectory = None
    target_screenshot = None
    if args.trajectory_id:
        for run in runs:
            if run.trajectory_id == args.trajectory_id:
                target_trajectory = run
                break
        if target_trajectory is None:
            print(f"run not found: {args.trajectory_id}", file=sys.stderr)
            return 1
    else:
        target_trajectory = runs[0]

    for step in target_trajectory.steps:
        name = step.observation.screenshot
        if not name:
            continue
        if args.filename and name != args.filename:
            continue
        bytes_ = storage.load_screenshot(target_trajectory.trajectory_id, name)
        if bytes_:
            target_screenshot = (step, name, bytes_)
            break

    if target_screenshot is None:
        print("no screenshot found for target run", file=sys.stderr)
        return 1

    step, name, image_bytes = target_screenshot
    print(f"trajectory_id     : {target_trajectory.trajectory_id}")
    print(f"step.index : {step.index}")
    print(f"screenshot : {name}  ({len(image_bytes):,} bytes)")
    print()

    client = llm.get_vlm_client()
    print(f"client     : {type(client).__name__}")
    print(f"model_name : {client.model_name}")
    print()

    print("--- low-detail call ---")
    low = client.summarize_low_detail(
        image_bytes,
        image_name=name,
        action_type=step.action.type,
        step_index=step.index,
    )
    print(f"result     : {low!r}")
    print()

    print("--- high-detail call ---")
    high = client.summarize_high_detail(
        image_bytes,
        image_name=name,
        action_type=step.action.type,
        step_index=step.index,
    )
    print(f"result     : {high!r}")

    if low is None or high is None:
        print()
        print("FAIL — at least one VLM call returned None. See WARNING log above.", file=sys.stderr)
        return 1
    print()
    print("OK — both calls returned a string summary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
