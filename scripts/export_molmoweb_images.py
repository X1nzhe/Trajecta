#!/usr/bin/env python3
"""Export screenshots from a materialized MolmoWeb-HumanSkills subset.

The actual image bytes come from the `images` column. `image_paths` is only an
optional naming/matching helper and may be null in materialized rows.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import mimetypes
import os
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Any


DEFAULT_INPUT_DIR = "data/raw/molmoweb_humanskills_sample/hf_dataset"
DEFAULT_OUTPUT_DIR = "data/raw/molmoweb_humanskills_sample/image_gallery"
VALID_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def load_materialized_dataset(input_dir: Path) -> Any:
    try:
        from datasets import load_from_disk
    except ImportError:
        print(
            "Missing dependency: install Hugging Face datasets first, e.g. "
            "`pip install datasets python-dotenv`.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return load_from_disk(str(input_dir))


def safe_filename(value: str, fallback: str) -> str:
    name = Path(value).name if value else fallback
    if not name:
        name = fallback
    if VALID_FILENAME_RE.fullmatch(name):
        return name
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned or fallback


def normalize_image_bytes(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, dict):
        for key in ("bytes", "data"):
            nested = value.get(key)
            if isinstance(nested, bytes):
                return nested
            if isinstance(nested, bytearray):
                return bytes(nested)
        path = value.get("path")
        if isinstance(path, str) and Path(path).exists():
            return Path(path).read_bytes()
    if hasattr(value, "save"):
        buffer = io.BytesIO()
        image_format = getattr(value, "format", None) or "PNG"
        try:
            value.save(buffer, format=image_format)
        except Exception:
            buffer = io.BytesIO()
            value.save(buffer, format="PNG")
        return buffer.getvalue()
    return None


def image_extension(filename: str, image_bytes: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return suffix
    if image_bytes.startswith(b"\x89PNG"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        return ".webp"
    guessed = mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "")
    return guessed or ".png"


def image_dimensions(image_bytes: bytes) -> tuple[int | None, int | None]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        width, height = struct.unpack(">II", image_bytes[16:24])
        return int(width), int(height)
    if image_bytes.startswith(b"\xff\xd8\xff"):
        index = 2
        while index + 9 < len(image_bytes):
            if image_bytes[index] != 0xFF:
                index += 1
                continue
            marker = image_bytes[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(image_bytes):
                break
            segment_length = int.from_bytes(image_bytes[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(image_bytes):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if segment_length >= 7:
                    height = int.from_bytes(image_bytes[index + 3 : index + 5], "big")
                    width = int.from_bytes(image_bytes[index + 5 : index + 7], "big")
                    return width, height
                break
            index += segment_length
    return None, None


def image_quality_flags(image_bytes: bytes, width: int | None, height: int | None) -> list[str]:
    flags: list[str] = []
    if len(image_bytes) < 20_000:
        flags.append("low_bytes")
    if width is not None and height is not None:
        if width < 600 or height < 400:
            flags.append("small_dimensions")
        if width * height < 300_000:
            flags.append("low_pixel_count")
    else:
        flags.append("unknown_dimensions")
    return flags


def parse_trajectory(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_task(instruction: Any) -> str:
    if isinstance(instruction, str):
        try:
            parsed = json.loads(instruction)
        except json.JSONDecodeError:
            return instruction.strip()
    else:
        parsed = instruction

    if isinstance(parsed, dict):
        for key in ("low_level", "mid_level", "high_level", "task", "instruction", "goal"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)

    return str(parsed).strip() if parsed is not None else ""


def json_text(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=indent, sort_keys=False)


def compact_json(value: Any, max_chars: int = 1400) -> str:
    text = json_text(value)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def action_summary(action: Any) -> str:
    if isinstance(action, str):
        return action
    if not isinstance(action, dict):
        return compact_json(action)

    parts: list[str] = []
    for key in ("action_str", "action_description"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    output = action.get("action_output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            parts.append(output)
            output = None
    if isinstance(output, dict):
        action_name = output.get("action_name") or output.get("name")
        if action_name:
            parts.append(str(action_name))
        params = {
            key: value
            for key, value in output.items()
            if key not in {"thought", "action_name", "name"} and value not in (None, "")
        }
        if params:
            parts.append(compact_json(params, max_chars=500))

    return " | ".join(parts) if parts else compact_json(action)


def step_summaries(row: dict[str, Any]) -> list[dict[str, Any]]:
    trajectory = parse_trajectory(row.get("trajectory"))
    if not isinstance(trajectory, dict):
        return []

    try:
        step_keys = sorted(trajectory.keys(), key=int)
    except (TypeError, ValueError):
        step_keys = list(trajectory.keys())

    steps: list[dict[str, Any]] = []
    for step_key in step_keys:
        step = trajectory.get(step_key)
        if not isinstance(step, dict):
            continue

        other_obs = step.get("other_obs") or {}
        url = None
        title = None
        if isinstance(other_obs, dict):
            url = other_obs.get("url") or other_obs.get("current_url")
            page_index = other_obs.get("page_index")
            titles = other_obs.get("open_pages_titles")
            if isinstance(titles, list) and isinstance(page_index, int) and 0 <= page_index < len(titles):
                title = titles[page_index]

        steps.append(
            {
                "index": step_key,
                "screenshot": step.get("screenshot"),
                "action": action_summary(step.get("action")),
                "url": url,
                "title": title,
                "timestamp": step.get("action_timestamp"),
                "raw": json_text(step, indent=2),
            }
        )
    return steps


def ordered_screenshot_refs(row: dict[str, Any]) -> list[str]:
    trajectory = parse_trajectory(row.get("trajectory"))
    if not isinstance(trajectory, dict):
        return []

    refs: list[str] = []
    try:
        step_keys = sorted(trajectory.keys(), key=int)
    except (TypeError, ValueError):
        step_keys = list(trajectory.keys())

    for step_key in step_keys:
        step = trajectory.get(step_key)
        if isinstance(step, dict) and step.get("screenshot"):
            refs.append(str(step["screenshot"]))
    return refs


def export_images(dataset: Any, output_dir: Path, overwrite: bool, only_referenced: bool) -> dict[str, Any]:
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}; pass --overwrite to replace it")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported_images: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for row_index, row in enumerate(dataset):
        row_dict = dict(row)
        sample_id = str(row_dict.get("sample_id") or f"row_{row_index}")
        task = extract_task(row_dict.get("instruction"))
        trajectory = parse_trajectory(row_dict.get("trajectory"))
        steps = step_summaries(row_dict)
        sample_dir = output_dir / safe_filename(sample_id, f"row_{row_index}")
        sample_dir.mkdir(parents=True, exist_ok=True)
        sample_images: list[dict[str, Any]] = []

        images = row_dict.get("images") or []
        raw_image_paths = row_dict.get("image_paths")
        image_paths = raw_image_paths if isinstance(raw_image_paths, list) else []
        ordered_refs = ordered_screenshot_refs(row_dict)
        refs = set(ordered_refs)
        ref_names = {Path(ref).name for ref in refs}
        can_match_image_paths = bool(image_paths)

        for image_index, image_value in enumerate(images):
            if image_index < len(image_paths):
                source_path = str(image_paths[image_index])
            elif image_index < len(ordered_refs):
                source_path = ordered_refs[image_index]
            else:
                source_path = f"image_{image_index}.png"

            source_path_matches_ref = source_path in refs or Path(source_path).name in ref_names
            if only_referenced and can_match_image_paths and refs and not source_path_matches_ref:
                continue

            image_bytes = normalize_image_bytes(image_value)
            if not image_bytes:
                continue

            fallback = f"image_{image_index}.png"
            base_name = safe_filename(source_path, fallback)
            suffix = image_extension(base_name, image_bytes)
            if not base_name.lower().endswith(suffix):
                base_name = f"{Path(base_name).stem}{suffix}"
            output_path = sample_dir / base_name
            output_path.write_bytes(image_bytes)
            width, height = image_dimensions(image_bytes)
            quality_flags = image_quality_flags(image_bytes, width, height)

            image_record = {
                "sample_id": sample_id,
                "source_path": source_path,
                "output_path": str(output_path),
                "relative_path": str(output_path.relative_to(output_dir)),
                "bytes": len(image_bytes),
                "width": width,
                "height": height,
                "quality_flags": quality_flags,
                "referenced": source_path_matches_ref,
            }
            exported_images.append(image_record)
            sample_images.append(image_record)

        sample_flags = sorted({flag for item in sample_images for flag in item.get("quality_flags") or []})
        samples.append(
            {
                "sample_id": sample_id,
                "task": task,
                "steps": steps,
                "trajectory_json": json_text(trajectory, indent=2) if trajectory else "",
                "images": sample_images,
                "image_count": len(sample_images),
                "flagged_image_count": sum(1 for item in sample_images if item.get("quality_flags")),
                "quality_flags": sample_flags,
            }
        )

    return {"samples": samples, "images": exported_images}


def write_gallery(output_dir: Path, gallery_data: dict[str, Any]) -> None:
    samples = gallery_data.get("samples") or []
    exported_images = gallery_data.get("images") or []

    sections: list[str] = []
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        task = str(sample.get("task") or "")
        images = sample.get("images") or []
        image_by_ref: dict[str, dict[str, Any]] = {}
        for item in images:
            source_path = str(item.get("source_path") or "")
            image_by_ref[source_path] = item
            image_by_ref[Path(source_path).name] = item

        step_cards: list[str] = []
        used_relative_paths: set[str] = set()
        for step in sample.get("steps") or []:
            screenshot = str(step.get("screenshot") or "")
            image_item = image_by_ref.get(screenshot) or image_by_ref.get(Path(screenshot).name)
            if image_item:
                used_relative_paths.add(str(image_item.get("relative_path") or ""))
                rel = html.escape(str(image_item["relative_path"]))
                dimensions = (
                    f"{image_item['width']}x{image_item['height']}"
                    if image_item.get("width") and image_item.get("height")
                    else "unknown size"
                )
                meta = f"{dimensions}, {int(image_item['bytes']) // 1024} KB"
                flags = ", ".join(image_item.get("quality_flags") or [])
                quality = f"<span class=\"flags\">{html.escape(flags)}</span>" if flags else ""
                media = (
                    f'<a href="{rel}"><img src="{rel}" loading="lazy"></a>'
                    f'<div class="image-meta">{html.escape(meta)}{quality}</div>'
                )
            else:
                media = '<div class="missing-image">No screenshot</div>'

            url = f'<div class="url">{html.escape(str(step.get("url") or ""))}</div>' if step.get("url") else ""
            title = f'<div class="title">{html.escape(str(step.get("title") or ""))}</div>' if step.get("title") else ""
            timestamp = (
                f'<span class="timestamp">{html.escape(str(step.get("timestamp")))}</span>'
                if step.get("timestamp") is not None
                else ""
            )
            step_cards.append(
                '<article class="step">'
                '<div class="step-body">'
                f'<div class="step-head"><strong>Step {html.escape(str(step.get("index")))}</strong>{timestamp}</div>'
                f'<div class="action">{html.escape(str(step.get("action") or ""))}</div>'
                f"{title}{url}"
                f'<details><summary>Raw step</summary><pre>{html.escape(str(step.get("raw") or ""))}</pre></details>'
                "</div>"
                f'<div class="media">{media}</div>'
                "</article>"
            )

        if not step_cards:
            step_cards.append('<div class="no-steps">No parseable trajectory steps</div>')

        extra_images: list[str] = []
        for item in images:
            rel_path = str(item.get("relative_path") or "")
            if rel_path in used_relative_paths:
                continue
            rel = html.escape(rel_path)
            extra_images.append(f'<a href="{rel}"><img src="{rel}" loading="lazy"></a>')
        extras = (
            f'<div class="extra-images"><h3>Unmatched images</h3><div>{"".join(extra_images)}</div></div>'
            if extra_images
            else ""
        )

        flags = ", ".join(sample.get("quality_flags") or [])
        sample_meta = (
            f"{len(sample.get('steps') or [])} steps, {int(sample.get('image_count') or 0)} images"
            f"{', flags: ' + flags if flags else ''}"
        )
        trajectory_json = str(sample.get("trajectory_json") or "")
        trajectory_details = (
            f'<details class="trajectory-json"><summary>Full trajectory JSON</summary>'
            f"<pre>{html.escape(trajectory_json)}</pre></details>"
            if trajectory_json
            else ""
        )
        sections.append(
            "<section>"
            f"<h2>{html.escape(sample_id)}</h2>"
            f"<p class=\"task\">{html.escape(task)}</p>"
            f"<p class=\"sample-meta\">{html.escape(sample_meta)}</p>"
            f"{trajectory_details}"
            f"<div class=\"steps\">{''.join(step_cards)}</div>"
            f"{extras}"
            "</section>"
        )

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MolmoWeb Image Gallery</title>
  <style>
    body {{ margin: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1f2933; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    h2 {{ margin: 28px 0 6px; font-size: 16px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }}
    .task {{ margin: 0 0 8px; max-width: 1100px; font-size: 14px; line-height: 1.45; color: #52606d; }}
    .sample-meta {{ margin: 0 0 12px; font-size: 12px; color: #829ab1; }}
    .steps {{ display: grid; gap: 12px; }}
    .step {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(180px, 260px); gap: 14px; align-items: start; border: 1px solid #d9e2ec; border-radius: 6px; padding: 10px; background: #fff; }}
    .media img {{ display: block; width: 100%; max-height: 190px; object-fit: contain; background: #f5f7fa; border-radius: 4px; }}
    .image-meta {{ margin-top: 6px; font-size: 12px; color: #627d98; overflow-wrap: anywhere; }}
    .missing-image {{ min-height: 120px; display: grid; place-items: center; color: #829ab1; background: #f5f7fa; border-radius: 4px; font-size: 13px; }}
    .step-head {{ display: flex; gap: 10px; align-items: baseline; margin-bottom: 6px; }}
    .timestamp {{ color: #829ab1; font-size: 12px; }}
    .action {{ font-size: 14px; line-height: 1.45; margin-bottom: 8px; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .title {{ font-size: 13px; color: #334e68; margin-bottom: 3px; }}
    .url {{ font-size: 12px; color: #52606d; overflow-wrap: anywhere; margin-bottom: 8px; }}
    details {{ margin-top: 8px; }}
    summary {{ cursor: pointer; color: #486581; font-size: 12px; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; max-height: 240px; overflow: auto; padding: 8px; background: #f5f7fa; border-radius: 4px; font-size: 11px; }}
    .trajectory-json pre {{ max-height: 420px; }}
    .flags {{ display: inline-block; margin-top: 4px; color: #9f580a; font-weight: 600; }}
    .no-steps {{ color: #829ab1; background: #f5f7fa; border-radius: 4px; padding: 12px; font-size: 13px; }}
    .extra-images {{ margin-top: 12px; }}
    .extra-images h3 {{ margin: 0 0 8px; font-size: 13px; color: #486581; }}
    .extra-images div {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }}
    .extra-images img {{ width: 100%; max-height: 120px; object-fit: contain; background: #f5f7fa; border-radius: 4px; }}
    @media (max-width: 760px) {{ .step {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <h1>MolmoWeb Image Gallery</h1>
  <p>{len(samples)} samples, {len(exported_images)} exported images</p>
  {''.join(sections)}
</body>
</html>
"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def write_manifest(output_dir: Path, gallery_data: dict[str, Any]) -> None:
    samples = gallery_data.get("samples") or []
    exported = gallery_data.get("images") or []
    by_flag: dict[str, int] = {}
    for item in exported:
        for flag in item.get("quality_flags") or []:
            by_flag[flag] = by_flag.get(flag, 0) + 1

    by_sample: dict[str, dict[str, Any]] = {}
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        by_sample[sample_id] = {
            "sample_id": sample_id,
            "task": sample.get("task") or "",
            "step_count": len(sample.get("steps") or []),
            "image_count": int(sample.get("image_count") or 0),
            "flagged_image_count": int(sample.get("flagged_image_count") or 0),
            "quality_flags": sample.get("quality_flags") or [],
        }

    manifest = {
        "sample_count": len(samples),
        "exported_count": len(exported),
        "flag_counts": dict(sorted(by_flag.items())),
        "samples": dict(sorted(by_sample.items())),
        "images": exported,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export MolmoWeb screenshot bytes to viewable image files.")
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--all-images",
        action="store_true",
        help="Export every image in each row. By default, export only screenshots referenced by trajectory when refs exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset = load_materialized_dataset(Path(args.input_dir))
    exported = export_images(
        dataset=dataset,
        output_dir=Path(args.output_dir),
        overwrite=args.overwrite,
        only_referenced=not args.all_images,
    )
    write_gallery(Path(args.output_dir), exported)
    write_manifest(Path(args.output_dir), exported)
    print(f"Exported {len(exported.get('images') or [])} images to {args.output_dir}")
    print(f"Open {Path(args.output_dir) / 'index.html'}")
    print(f"Wrote manifest to {Path(args.output_dir) / 'manifest.json'}")
    return 0


def exit_process(code: int) -> None:
    # HF datasets can leave background workers alive after the useful work is done.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    exit_process(main())
