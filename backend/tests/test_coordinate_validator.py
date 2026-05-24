from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from backend.app.coordinate_validator import validate_coordinates
from backend.app.schemas import (
    BBox,
    Coordinate,
    StepAction,
    StepObservation,
    TrajectoryStep,
)


class CoordinateValidatorTests(unittest.TestCase):
    def test_missing_coordinates(self) -> None:
        result = validate_coordinates(StepAction(type="scroll"), image_width=100, image_height=80)

        self.assertEqual(result.status, "missing")
        self.assertEqual(result.image_width, 100)
        self.assertIn("no coordinates", result.reason or "")

    def test_unknown_dimensions(self) -> None:
        result = validate_coordinates(
            StepAction(type="click", coordinates=Coordinate(x=10, y=20)),
        )

        self.assertEqual(result.status, "unknown")
        self.assertIn("dimensions unavailable", result.reason or "")

    def test_validated_in_bounds(self) -> None:
        result = validate_coordinates(
            StepAction(type="click", coordinates=Coordinate(x=10, y=20)),
            image_width=100,
            image_height=80,
        )

        self.assertEqual(result.status, "validated")
        self.assertEqual(result.image_height, 80)

    def test_out_of_bounds(self) -> None:
        result = validate_coordinates(
            StepAction(type="click", coordinates=Coordinate(x=101, y=20)),
            image_width=100,
            image_height=80,
        )

        self.assertEqual(result.status, "out_of_bounds")
        self.assertIn("outside image bounds", result.reason or "")

    def test_bbox_overlay_only_drawn_when_bounds_valid(self) -> None:
        """docs/contracts.md L184: 'StepAction.bbox is untrusted unless
        validated against screenshot dimensions.' The backend signals this by
        flagging the row as out_of_bounds via CoordinateValidation so the
        frontend skips overlay rendering — see docs/frontend.md.
        """

        action = StepAction(
            type="click",
            coordinates=Coordinate(x=150, y=120),  # outside the 100x80 screenshot
            bbox=BBox(x=140, y=110, width=200, height=200),  # exceeds 100x80
        )
        step = TrajectoryStep(
            index=0,
            observation=StepObservation(screenshot="screenshot_001.png"),
            action=action,
        )

        result = validate_coordinates(step.action, image_width=100, image_height=80)

        self.assertEqual(result.status, "out_of_bounds")
        self.assertEqual(result.image_width, 100)
        self.assertEqual(result.image_height, 80)

    def test_reads_dimensions_from_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "step.png"
            Image.new("RGB", (64, 48)).save(image_path)

            result = validate_coordinates(
                StepAction(type="click", coordinates=Coordinate(x=63, y=47)),
                image_path=image_path,
            )

        self.assertEqual(result.status, "validated")
        self.assertEqual(result.image_width, 64)
        self.assertEqual(result.image_height, 48)


if __name__ == "__main__":
    unittest.main()
