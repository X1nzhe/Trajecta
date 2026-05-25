"""MolmoWeb-HumanSkills import helpers."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from backend.app.coordinate_validator import validate_coordinates
from backend.app.schemas import BBox, Coordinate, StepAction, StepObservation, StepResult, TrajectoryRun, TrajectoryStep


VALID_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
VALID_SCREENSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VALID_STATUSES = {"success", "failed", "unknown"}
_LAST_SCREENSHOT_ASSETS: dict[str, dict[str, bytes]] = {}


def _load_dataset_from_disk(source_dir: Path) -> Any:
    try:
        from datasets import load_from_disk
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: install `datasets` to import MolmoWeb samples from disk."
        ) from exc
    return load_from_disk(str(source_dir))


def import_sample(source_dir: Path) -> list[TrajectoryRun]:
    """Import a local Hugging Face Dataset saved with ``datasets.save_to_disk``.

    Screenshot bytes are kept in a module-local cache keyed by run ID. API code
    can call ``get_imported_screenshot_assets(run_id)`` immediately after this
    function returns, then persist the bytes through ``storage.save_screenshots``.
    The public return contract stays a list of JSON-serializable TrajectoryRun
    objects.
    """

    dataset = _load_dataset_from_disk(source_dir)
    runs: list[TrajectoryRun] = []
    _LAST_SCREENSHOT_ASSETS.clear()

    for row_index, row in enumerate(dataset):
        raw = dict(row)
        run_id = str(raw.get("sample_id") or "")
        if not run_id:
            raise ValueError(f"row {row_index}: missing sample_id")
        _validate_run_id(run_id)

        run = normalize_trajectory(raw, run_id=run_id)
        # docs/dataset_import.md "Cold-Start Behavior": every imported run
        # lands at status="unknown" regardless of any raw status / outcome
        # field in the source row. The Eval Agent must derive its own
        # verdict; pre-seeded labels would let the agent (or `get_run`)
        # copy the answer. normalize_trajectory still honors raw status
        # because ad-hoc scripts and tests rely on that path; the import
        # pipeline overrides it here.
        run = run.model_copy(update={"status": "unknown"})
        runs.append(run)
        assets = _extract_screenshot_assets(raw, run)
        if assets:
            _LAST_SCREENSHOT_ASSETS[run.run_id] = assets

    # apply_status_overlay() remains importable for ad-hoc scripts and
    # tests but is deliberately not called here.
    return runs


def get_imported_screenshot_assets(run_id: str) -> dict[str, bytes]:
    return dict(_LAST_SCREENSHOT_ASSETS.get(run_id, {}))


def normalize_trajectory(raw: dict[str, Any], run_id: str) -> TrajectoryRun:
    _validate_run_id(run_id)
    trajectory = _parse_trajectory(raw.get("trajectory"))
    task = _extract_task(raw.get("instruction"))
    step_keys = _sorted_step_keys(trajectory)

    steps: list[TrajectoryStep] = []
    for step_key in step_keys:
        raw_step = trajectory[step_key]
        if not isinstance(raw_step, dict):
            continue
        # Preserve the source dataset's 1-based step numbering. _sorted_step_keys
        # already validates that step keys are numeric strings, so int() is
        # safe. Aligning index with the source key (and with the 1-based
        # screenshot filenames like "screenshot_001.png") avoids the
        # off-by-one confusion of an enumerate-derived 0-based index where
        # the same step is referred to by three different numbers across
        # the stack (internal 0-based vs displayed 1-based vs filename
        # 1-based).
        index = int(step_key)

        other_obs = _as_dict(raw_step.get("other_obs"))
        action = parse_action(raw_step.get("action"))
        image_width = _optional_int(
            raw_step.get("image_w", raw_step.get("image_width", raw_step.get("width")))
        )
        image_height = _optional_int(
            raw_step.get("image_h", raw_step.get("image_height", raw_step.get("height")))
        )
        screenshot = _screenshot_name(raw_step.get("screenshot"))

        metadata: dict[str, Any] = {
            "source_step_key": str(step_key),
            "source_action_name": _source_action_name(raw_step.get("action")),
            "image_width": image_width,
            "image_height": image_height,
        }
        source_screenshot = raw_step.get("screenshot")
        if source_screenshot is not None and screenshot != str(source_screenshot):
            metadata["source_screenshot"] = str(source_screenshot)

        timestamp = raw_step.get("action_timestamp")
        steps.append(
            TrajectoryStep(
                index=index,
                timestamp=str(timestamp) if timestamp is not None else None,
                observation=StepObservation(
                    screenshot=screenshot,
                    url=_current_url(other_obs),
                    title=_current_title(other_obs),
                ),
                action=action,
                result=StepResult(status=_normalize_status(raw_step.get("status"))),
                coordinate_validation=validate_coordinates(
                    action,
                    image_width=image_width,
                    image_height=image_height,
                ),
                metadata=metadata,
            )
        )

    return TrajectoryRun(
        run_id=run_id,
        task=task,
        status=_normalize_status(raw.get("status") or raw.get("outcome")),
        steps=steps,
        metadata=_row_metadata(raw),
    )


def parse_action(raw_action: str | dict[str, Any] | Any) -> StepAction:
    try:
        if isinstance(raw_action, str):
            return _parse_action_string(raw_action)
        if isinstance(raw_action, dict):
            return _parse_action_dict(raw_action)
        return StepAction(type="unknown", raw=_json_text(raw_action))
    except Exception:
        return StepAction(type="unknown", raw=_json_text(raw_action))


def apply_status_overlay(runs: list[TrajectoryRun], overlay_path: Path) -> list[TrajectoryRun]:
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(overlay, dict):
        raise ValueError(f"status overlay must be a JSON object: {overlay_path}")

    normalized: dict[str, str] = {}
    for run_id, status in overlay.items():
        _validate_run_id(str(run_id))
        status_value = status.strip().lower() if isinstance(status, str) else ""
        if status_value not in VALID_STATUSES:
            raise ValueError(f"invalid status for {run_id}: {status!r}")
        normalized[str(run_id)] = status_value

    return [run.model_copy(update={"status": normalized[run.run_id]}) if run.run_id in normalized else run for run in runs]


def _validate_run_id(run_id: str) -> None:
    if not VALID_RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id {run_id!r}; must match {VALID_RUN_ID_RE.pattern}")


def _parse_trajectory(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("trajectory must be a JSON object or JSON-encoded object")


def _sorted_step_keys(trajectory: dict[str, Any]) -> list[str]:
    try:
        return sorted(trajectory.keys(), key=lambda key: int(key))
    except (TypeError, ValueError) as exc:
        raise ValueError("trajectory step keys must be numeric strings") from exc


def _extract_task(instruction: Any) -> str:
    parsed: Any = instruction
    if isinstance(instruction, str):
        try:
            parsed = json.loads(instruction)
        except json.JSONDecodeError:
            text = instruction.strip()
            return text or "Unspecified task"

    if isinstance(parsed, dict):
        for key in ("low_level", "mid_level", "high_level", "task", "instruction", "goal"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)

    if parsed is None:
        return "Unspecified task"
    return str(parsed).strip() or "Unspecified task"


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _current_url(other_obs: dict[str, Any]) -> str | None:
    for key in ("url", "current_url"):
        value = other_obs.get(key)
        if isinstance(value, str) and value:
            return value

    page_index = other_obs.get("page_index")
    urls = other_obs.get("open_pages_urls") or other_obs.get("open_pages")
    if isinstance(page_index, int) and isinstance(urls, list) and 0 <= page_index < len(urls):
        value = urls[page_index]
        return str(value) if value else None
    return None


def _current_title(other_obs: dict[str, Any]) -> str | None:
    for key in ("title", "current_title"):
        value = other_obs.get(key)
        if isinstance(value, str) and value:
            return value

    page_index = other_obs.get("page_index")
    titles = other_obs.get("open_pages_titles")
    if isinstance(page_index, int) and isinstance(titles, list) and 0 <= page_index < len(titles):
        value = titles[page_index]
        return str(value) if value else None
    return None


def _normalize_status(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_STATUSES:
            return normalized
        if normalized in {"failure", "error", "failed_task"}:
            return "failed"
        if normalized in {"complete", "completed", "passed", "pass"}:
            return "success"
    return "unknown"


def _row_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("category", "failure_mode", "notes"):
        value = raw.get(key)
        if value not in (None, ""):
            metadata[key] = value
    return metadata


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _screenshot_name(value: Any) -> str | None:
    if not value:
        return None
    name = Path(str(value)).name
    if not name:
        return None
    if VALID_SCREENSHOT_NAME_RE.fullmatch(name):
        return name
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned or None


def _source_action_name(raw_action: Any) -> str | None:
    if not isinstance(raw_action, dict):
        return None
    output = _action_output(raw_action)
    for source in (output, raw_action):
        if isinstance(source, dict):
            value = source.get("action_name") or source.get("name") or source.get("type")
            if value:
                return str(value)
    return None


def _parse_action_string(raw_action: str) -> StepAction:
    stripped = raw_action.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            parsed_action = _parse_action_dict(parsed)
            return parsed_action.model_copy(update={"raw": parsed_action.raw or raw_action})

    action_type = _action_type("", raw_action)
    x = _regex_number(raw_action, "x")
    y = _regex_number(raw_action, "y")
    text = _regex_quoted_value(raw_action, "text") or _regex_quoted_value(raw_action, "key")
    return StepAction(
        type=action_type,
        text=text,
        coordinates=Coordinate(x=x, y=y) if x is not None and y is not None else None,
        raw=raw_action,
    )


def _parse_action_dict(raw_action: dict[str, Any]) -> StepAction:
    output = _action_output(raw_action)
    params = output.get("action") if isinstance(output.get("action"), dict) else {}
    sources = [params, output, raw_action]

    raw = _action_raw_text(raw_action)
    name = " ".join(
        str(value)
        for value in (
            output.get("action_name") if isinstance(output, dict) else None,
            output.get("name") if isinstance(output, dict) else None,
            raw_action.get("action_name"),
            raw_action.get("name"),
            raw_action.get("type"),
        )
        if value
    )

    coordinates = _extract_coordinates(sources)
    bbox = _extract_bbox(sources)
    return StepAction(
        type=_action_type(name, raw),
        label=_first_text(raw_action, ("action_description", "label", "description")),
        text=_first_text_from_sources(sources, ("text", "key", "value", "input", "query")),
        coordinates=coordinates,
        bbox=bbox,
        raw=raw,
    )


def _action_output(raw_action: dict[str, Any]) -> dict[str, Any]:
    output = raw_action.get("action_output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return {}
    return output if isinstance(output, dict) else {}


def _action_raw_text(raw_action: dict[str, Any]) -> str:
    for key in ("action_str", "raw", "action_text"):
        value = raw_action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _json_text(raw_action)


def _action_type(name: str, raw: str) -> str:
    text = f"{name} {raw}".lower()
    if any(token in text for token in ("mouse_click", "click", "tap")):
        return "click"
    if any(token in text for token in ("keyboard_type", "input_text", "type", "fill")):
        return "type"
    if any(token in text for token in ("mouse_scroll", "scroll", "wheel")):
        return "scroll"
    if any(token in text for token in ("navigate", "goto", "go_to", "open_url", "visit")):
        return "navigate"
    if any(token in text for token in ("wait", "sleep")):
        return "wait"
    return "unknown"


def _extract_coordinates(sources: list[Any]) -> Coordinate | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        x = source.get("x")
        y = source.get("y")
        if _is_number(x) and _is_number(y):
            return Coordinate(x=float(x), y=float(y))

        coord = source.get("coordinates") or source.get("coordinate") or source.get("point")
        if isinstance(coord, dict) and _is_number(coord.get("x")) and _is_number(coord.get("y")):
            return Coordinate(x=float(coord["x"]), y=float(coord["y"]))
        if isinstance(coord, list) and len(coord) >= 2 and _is_number(coord[0]) and _is_number(coord[1]):
            return Coordinate(x=float(coord[0]), y=float(coord[1]))
    return None


def _extract_bbox(sources: list[Any]) -> BBox | None:
    for source in sources:
        if not isinstance(source, dict) or "bbox" not in source:
            continue
        bbox = source["bbox"]
        if isinstance(bbox, dict):
            x = bbox.get("x", bbox.get("left"))
            y = bbox.get("y", bbox.get("top"))
            width = bbox.get("width", bbox.get("w"))
            height = bbox.get("height", bbox.get("h"))
            if all(_is_number(value) for value in (x, y, width, height)):
                return BBox(x=float(x), y=float(y), width=float(width), height=float(height))
        if isinstance(bbox, list) and len(bbox) == 4 and all(_is_number(value) for value in bbox):
            return BBox(x=float(bbox[0]), y=float(bbox[1]), width=float(bbox[2]), height=float(bbox[3]))
    return None


def _first_text(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_text_from_sources(sources: list[Any], keys: tuple[str, ...]) -> str | None:
    for source in sources:
        if isinstance(source, dict):
            value = _first_text(source, keys)
            if value is not None:
                return value
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _regex_number(text: str, key: str) -> float | None:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _regex_quoted_value(text: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*(['\"])(.*?)\1", text)
    return match.group(2) if match else None


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _extract_screenshot_assets(raw: dict[str, Any], run: TrajectoryRun) -> dict[str, bytes]:
    images = raw.get("images") or []
    if not isinstance(images, list) or not images:
        return {}

    trajectory = _parse_trajectory(raw.get("trajectory"))
    step_keys = _sorted_step_keys(trajectory)
    refs = [
        (trajectory[key].get("screenshot"), _screenshot_name(trajectory[key].get("screenshot")))
        for key in step_keys
        if isinstance(trajectory[key], dict)
    ]

    raw_image_paths = raw.get("image_paths")
    image_paths = raw_image_paths if isinstance(raw_image_paths, list) else []
    assets: dict[str, bytes] = {}

    if image_paths:
        image_by_ref: dict[str, Any] = {}
        for source_path, image in zip(image_paths, images):
            source_text = str(source_path)
            image_by_ref[source_text] = image
            image_by_ref[Path(source_text).name] = image

        for source_ref, filename in refs:
            if not filename or source_ref is None:
                continue
            image = image_by_ref.get(str(source_ref)) or image_by_ref.get(Path(str(source_ref)).name)
            image_bytes = _normalize_image_bytes(image)
            if image_bytes:
                assets[filename] = image_bytes
    else:
        for (_, filename), image in zip(refs, images):
            if not filename:
                continue
            image_bytes = _normalize_image_bytes(image)
            if image_bytes:
                assets[filename] = image_bytes

    run_screenshots = {step.observation.screenshot for step in run.steps if step.observation.screenshot}
    return {name: data for name, data in assets.items() if name in run_screenshots}


def _normalize_image_bytes(value: Any) -> bytes | None:
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
        # Intentionally ignore any `path` field to avoid reading arbitrary local files from dataset rows.
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
