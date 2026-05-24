"""Coordinate validation helpers for trajectory actions."""

from __future__ import annotations

import io

from backend.app.schemas import CoordinateValidation, StepAction


def _read_image_dimensions(image_bytes: bytes | None) -> tuple[int | None, int | None]:
    if not image_bytes:
        return None, None

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as image:
        return int(image.width), int(image.height)


def validate_coordinates(
    action: StepAction,
    image_bytes: bytes | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
) -> CoordinateValidation:
    """Validate action coordinates against known screenshot dimensions."""

    width = image_width
    height = image_height
    if (width is None or height is None) and image_bytes:
        width, height = _read_image_dimensions(image_bytes)

    if action.coordinates is None:
        return CoordinateValidation(
            status="missing",
            image_width=width,
            image_height=height,
            reason="action has no coordinates",
        )

    if width is None or height is None:
        return CoordinateValidation(
            status="unknown",
            image_width=width,
            image_height=height,
            reason="image dimensions unavailable",
        )

    x = action.coordinates.x
    y = action.coordinates.y
    if 0 <= x < width and 0 <= y < height:
        return CoordinateValidation(status="validated", image_width=width, image_height=height)

    return CoordinateValidation(
        status="out_of_bounds",
        image_width=width,
        image_height=height,
        reason=f"coordinate ({x}, {y}) outside image bounds x in [0, {width}) y in [0, {height})",
    )
