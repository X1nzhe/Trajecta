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


def export_images(dataset: Any, output_dir: Path, overwrite: bool, only_referenced: bool) -> list[dict[str, Any]]:
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"output directory already exists: {output_dir}; pass --overwrite to replace it")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported: list[dict[str, Any]] = []
    for row_index, row in enumerate(dataset):
        row_dict = dict(row)
        sample_id = str(row_dict.get("sample_id") or f"row_{row_index}")
        task = extract_task(row_dict.get("instruction"))
        sample_dir = output_dir / safe_filename(sample_id, f"row_{row_index}")
        sample_dir.mkdir(parents=True, exist_ok=True)

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

            exported.append(
                {
                    "sample_id": sample_id,
                    "task": task,
                    "source_path": source_path,
                    "output_path": str(output_path),
                    "relative_path": str(output_path.relative_to(output_dir)),
                    "bytes": len(image_bytes),
                    "width": width,
                    "height": height,
                    "quality_flags": quality_flags,
                    "referenced": source_path_matches_ref,
                }
            )

    return exported


def write_gallery(output_dir: Path, exported: list[dict[str, Any]]) -> None:
    by_sample: dict[str, list[dict[str, Any]]] = {}
    for item in exported:
        by_sample.setdefault(str(item["sample_id"]), []).append(item)

    sections: list[str] = []
    for sample_id, items in by_sample.items():
        task = str(items[0].get("task") or "")
        cards = []
        for item in items:
            rel = html.escape(item["relative_path"])
            label = html.escape(Path(str(item["source_path"])).name)
            dimensions = (
                f"{item['width']}x{item['height']}" if item.get("width") and item.get("height") else "unknown size"
            )
            meta = f"{dimensions}, {int(item['bytes']) // 1024} KB"
            flags = ", ".join(item.get("quality_flags") or [])
            quality = f"<span class=\"flags\">{html.escape(flags)}</span>" if flags else ""
            cards.append(
                f'<figure><a href="{rel}"><img src="{rel}" loading="lazy"></a>'
                f"<figcaption>{label}<br><span>{html.escape(meta)}</span>{quality}</figcaption></figure>"
            )
        sections.append(
            "<section>"
            f"<h2>{html.escape(sample_id)}</h2>"
            f"<p class=\"task\">{html.escape(task)}</p>"
            f"<div class=\"grid\">{''.join(cards)}</div>"
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
    .task {{ margin: 0 0 12px; max-width: 1100px; font-size: 14px; line-height: 1.45; color: #52606d; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }}
    figure {{ margin: 0; border: 1px solid #d9e2ec; border-radius: 6px; overflow: hidden; background: #fff; }}
    img {{ display: block; width: 100%; height: 180px; object-fit: contain; background: #f5f7fa; }}
    figcaption {{ padding: 8px 10px; font-size: 12px; overflow-wrap: anywhere; }}
    figcaption span {{ color: #627d98; }}
    .flags {{ display: inline-block; margin-top: 4px; color: #9f580a; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>MolmoWeb Image Gallery</h1>
  <p>{len(exported)} exported images</p>
  {''.join(sections)}
</body>
</html>
"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def write_manifest(output_dir: Path, exported: list[dict[str, Any]]) -> None:
    by_flag: dict[str, int] = {}
    for item in exported:
        for flag in item.get("quality_flags") or []:
            by_flag[flag] = by_flag.get(flag, 0) + 1

    manifest = {
        "exported_count": len(exported),
        "flag_counts": dict(sorted(by_flag.items())),
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
    print(f"Exported {len(exported)} images to {args.output_dir}")
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
