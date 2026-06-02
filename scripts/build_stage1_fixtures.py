#!/usr/bin/env python3
"""Build small normalized Stage 1 fixtures from the local image gallery.

This script intentionally uses only the Python standard library. It consumes the
HTML/manifest produced by ``scripts/export_molmoweb_images.py`` and writes
contract-valid Trajecta run fixtures under ``data/runs``.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any


DEFAULT_GALLERY_DIR = Path("data/raw/molmoweb_humanskills_sample/image_gallery")
DEFAULT_TRIAGE_CSV = Path("data/triage_notes.csv")
DEFAULT_OVERLAY = Path("data/raw/molmoweb_humanskills_sample/run_status_overlay.json")
DEFAULT_OUTPUT_DIR = Path("data/runs")

SELECTED_RUN_IDS = [
    "87ea181fa8c78cf62748a3490a845f4740dd4d5824cda20316ec05022998059e",  # google_flight success
    "bb468f6017ef274f475ea5caabf34ac6c33f2523ae92e993087b4a9cfd2619d8",  # google_flight failed
    "3672b077c54192ee2e018d1910a8c06b38e779e80e27ea7f169d586b6e52ee01",  # amazon success
    "865eb899d535f41df5bf4b17d84eaf0ab7adea06704ad24a5fea56598831e7fa",  # apple success
    "a492a7f130f565cc31662ce63c5ed1297ff48df996a747d735405df2269a3bfb",  # allrecipes success
    "32e7dbe84bcaf8206d7d28a43e9c7b26e3553c33e509c30618bddb34fe8aaef4",  # github success
    "b31ed3c90690a0e0757c108be0eafd941a32358a396f9a01d9dfeb4f1b279158",  # booking success
]


SECTION_RE = re.compile(
    r"<section><h2>(?P<trajectory_id>[^<]+)</h2>"
    r'<p class="task">(?P<task>.*?)</p>.*?'
    r'<details class="trajectory-json"><summary>Full trajectory JSON</summary>'
    r"<pre>(?P<trajectory>.*?)</pre>",
    re.S,
)


def read_triage(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            sample_id = (row.get("sample_id") or "").strip()
            if sample_id:
                rows[sample_id] = {
                    "category": (row.get("category") or "").strip(),
                    "failure_mode": (row.get("failure_mode") or "").strip(),
                    "notes": (row.get("notes") or "").strip(),
                }
    return rows


def load_gallery_sections(index_path: Path) -> dict[str, dict[str, Any]]:
    text = index_path.read_text(encoding="utf-8")
    sections: dict[str, dict[str, Any]] = {}
    for match in SECTION_RE.finditer(text):
        trajectory_id = html.unescape(match.group("trajectory_id")).strip()
        task = html.unescape(match.group("task")).strip()
        trajectory_text = html.unescape(match.group("trajectory"))
        sections[trajectory_id] = {
            "task": task,
            "trajectory": json.loads(trajectory_text),
        }
    return sections


def action_output(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {}
    output = action.get("action_output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return {}
    return output if isinstance(output, dict) else {}


def action_type(name: str, raw: str) -> str:
    name = name.lower()
    raw = raw.lower()
    if name == "click" or "mouse_click" in raw:
        return "click"
    if name in {"keyboard_type", "type"} or "keyboard_type" in raw:
        return "type"
    if name in {"scroll", "mouse_scroll"} or "scroll" in raw:
        return "scroll"
    if name in {"navigate", "goto", "open"} or raw.startswith(("goto", "navigate")):
        return "navigate"
    if name in {"wait", "sleep"} or raw.startswith(("wait", "sleep")):
        return "wait"
    return "unknown"


def normalize_action(raw_action: Any) -> dict[str, Any]:
    if not isinstance(raw_action, dict):
        return {"type": "unknown", "raw": json.dumps(raw_action, default=str)}

    output = action_output(raw_action)
    raw = str(raw_action.get("action_str") or "")
    name = str(output.get("action_name") or raw_action.get("action_name") or "")
    params = output.get("action")
    params = params if isinstance(params, dict) else {}

    normalized: dict[str, Any] = {
        "type": action_type(name, raw),
        "label": raw_action.get("action_description"),
        "raw": raw or json.dumps(raw_action, default=str, sort_keys=True),
    }
    if "text" in params and params["text"] is not None:
        normalized["text"] = str(params["text"])
    if "key" in params and params["key"] is not None and normalized["type"] == "unknown":
        normalized["text"] = str(params["key"])

    x = params.get("x")
    y = params.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        normalized["coordinates"] = {"x": float(x), "y": float(y)}

    bbox = params.get("bbox")
    if (
        isinstance(bbox, list)
        and len(bbox) == 4
        and all(isinstance(value, (int, float)) for value in bbox)
    ):
        normalized["bbox"] = {
            "x": float(bbox[0]),
            "y": float(bbox[1]),
            "width": float(bbox[2]),
            "height": float(bbox[3]),
        }

    return normalized


def current_title(other_obs: dict[str, Any]) -> str | None:
    page_index = other_obs.get("page_index")
    titles = other_obs.get("open_pages_titles")
    if isinstance(page_index, int) and isinstance(titles, list) and 0 <= page_index < len(titles):
        value = titles[page_index]
        return str(value) if value is not None else None
    return None


def coord_validation(action: dict[str, Any], image_width: Any, image_height: Any) -> dict[str, Any]:
    width = int(image_width) if isinstance(image_width, int) else None
    height = int(image_height) if isinstance(image_height, int) else None
    coord = action.get("coordinates")
    if coord is None:
        return {"status": "missing", "image_width": width, "image_height": height, "reason": "action has no coordinate"}
    if width is None or height is None:
        return {"status": "unknown", "image_width": width, "image_height": height, "reason": "image dimensions unavailable"}
    x = coord["x"]
    y = coord["y"]
    if 0 <= x <= width and 0 <= y <= height:
        return {"status": "validated", "image_width": width, "image_height": height}
    return {
        "status": "out_of_bounds",
        "image_width": width,
        "image_height": height,
        "reason": f"coordinate ({x}, {y}) outside {width}x{height}",
    }


def build_run(
    trajectory_id: str,
    section: dict[str, Any],
    overlay: dict[str, str],
    triage: dict[str, dict[str, str]],
    gallery_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    run_dir = output_dir / trajectory_id
    screenshot_dir = run_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    trajectory = section["trajectory"]
    step_keys = sorted(trajectory.keys(), key=lambda key: int(key))
    steps = []
    for zero_index, step_key in enumerate(step_keys):
        raw_step = trajectory[step_key]
        other_obs = raw_step.get("other_obs") or {}
        if not isinstance(other_obs, dict):
            other_obs = {}

        screenshot = raw_step.get("screenshot")
        if screenshot:
            source = gallery_dir / trajectory_id / Path(str(screenshot)).name
            if source.exists():
                shutil.copy2(source, screenshot_dir / Path(str(screenshot)).name)

        action = normalize_action(raw_step.get("action"))
        image_w = raw_step.get("image_w")
        image_h = raw_step.get("image_h")
        steps.append(
            {
                "index": zero_index,
                "timestamp": str(raw_step["action_timestamp"]) if raw_step.get("action_timestamp") is not None else None,
                "observation": {
                    "screenshot": Path(str(screenshot)).name if screenshot else None,
                    "url": other_obs.get("url") or other_obs.get("current_url"),
                    "title": current_title(other_obs),
                    "visible_text": None,
                    "visual_evidence": [],
                },
                "action": action,
                "result": {"status": "unknown", "error": None},
                "coordinate_validation": coord_validation(action, image_w, image_h),
                "metadata": {
                    "source_step_key": step_key,
                    "source_action_name": action_output(raw_step.get("action")).get("action_name"),
                    "image_width": image_w,
                    "image_height": image_h,
                },
            }
        )

    triage_row = triage.get(trajectory_id, {})
    return {
        "trajectory_id": trajectory_id,
        "task": section["task"],
        "source": "allenai/MolmoWeb-HumanSkills",
        "status": overlay.get(trajectory_id, "unknown"),
        "steps": steps,
        "metadata": {
            "category": triage_row.get("category") or None,
            "failure_mode": triage_row.get("failure_mode") or None,
            "notes": triage_row.get("notes") or None,
            "fixture_set": "stage1",
        },
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gallery-dir", type=Path, default=DEFAULT_GALLERY_DIR)
    parser.add_argument("--triage-csv", type=Path, default=DEFAULT_TRIAGE_CSV)
    parser.add_argument("--status-overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sections = load_gallery_sections(args.gallery_dir / "index.html")
    triage = read_triage(args.triage_csv)
    overlay = json.loads(args.status_overlay.read_text(encoding="utf-8"))

    for trajectory_id in SELECTED_RUN_IDS:
        if trajectory_id not in sections:
            raise KeyError(f"{trajectory_id} not found in {args.gallery_dir / 'index.html'}")
        run = build_run(trajectory_id, sections[trajectory_id], overlay, triage, args.gallery_dir, args.output_dir)
        write_json(args.output_dir / trajectory_id / "trajectory.json", run)
        print(f"wrote {trajectory_id}: {len(run['steps'])} steps, status={run['status']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
