from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app import dataset_importer


def raw_row(**overrides):
    row = {
        "sample_id": "run_1",
        "instruction": json.dumps(
            {
                "low_level": "Find the checkout button.",
                "mid_level": "Shop for an item.",
            }
        ),
        "trajectory": json.dumps(
            {
                "2": {
                    "screenshot": "screenshot_002.png",
                    "action": {"action_str": "wait()", "action_output": {"action_name": "wait"}},
                    "other_obs": {"url": "https://example.com/two", "title": "Two"},
                },
                "1": {
                    "screenshot": "screenshot_001.png",
                    "action": {
                        "action_str": "mouse_click(x=10, y=20, button='left')",
                        "action_description": "Click checkout",
                        "action_output": json.dumps(
                            {
                                "action_name": "click",
                                "action": {"x": 10, "y": 20, "bbox": [1, 2, 3, 4]},
                            }
                        ),
                    },
                    "other_obs": {"current_url": "https://example.com/one", "current_title": "One"},
                    "image_w": 100,
                    "image_h": 80,
                    "action_timestamp": 123.4,
                },
            }
        ),
        "images": [b"first", b"second"],
        "image_paths": None,
    }
    row.update(overrides)
    return row


class DatasetImporterTests(unittest.TestCase):
    def test_parse_click_action(self) -> None:
        action = dataset_importer.parse_action(
            {
                "action_str": "mouse_click(x=10, y=20, button='left')",
                "action_description": "Click result",
                "action_output": json.dumps(
                    {"action_name": "click", "action": {"x": 10, "y": 20, "bbox": [1, 2, 3, 4]}}
                ),
            }
        )

        self.assertEqual(action.type, "click")
        self.assertEqual(action.coordinates.x, 10)
        self.assertEqual(action.bbox.width, 3)
        self.assertEqual(action.label, "Click result")

    def test_parse_type_action(self) -> None:
        action = dataset_importer.parse_action(
            {
                "action_str": "keyboard_type(text='hello')",
                "action_output": {"action_name": "keyboard_type", "action": {"text": "hello"}},
            }
        )

        self.assertEqual(action.type, "type")
        self.assertEqual(action.text, "hello")

    def test_parse_malformed_unknown_action(self) -> None:
        action = dataset_importer.parse_action({"action_str": "unhandled()", "action_output": "{not-json"})

        self.assertEqual(action.type, "unknown")
        self.assertEqual(action.raw, "unhandled()")

    def test_normalize_basic_trajectory_row(self) -> None:
        run = dataset_importer.normalize_trajectory(raw_row(), trajectory_id="run_1")

        self.assertEqual(run.task, "Find the checkout button.")
        self.assertEqual([step.metadata["source_step_key"] for step in run.steps], ["1", "2"])
        # 1-based step.index aligned with the source step keys.
        self.assertEqual([step.index for step in run.steps], [1, 2])
        self.assertEqual(run.steps[0].observation.url, "https://example.com/one")
        self.assertEqual(run.steps[0].coordinate_validation.status, "validated")
        self.assertEqual(run.steps[0].action.raw, "mouse_click(x=10, y=20, button='left')")
        self.assertEqual(run.steps[1].action.type, "wait")

    def test_task_extraction_from_instruction_json(self) -> None:
        row = raw_row(instruction=json.dumps({"mid_level": "Use search", "goal": "Find result"}))
        run = dataset_importer.normalize_trajectory(row, trajectory_id="run_1")

        self.assertEqual(run.task, "Use search")

    def test_apply_status_overlay(self) -> None:
        run = dataset_importer.normalize_trajectory(raw_row(), trajectory_id="run_1")
        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = Path(tmpdir) / "run_status_overlay.json"
            overlay_path.write_text(json.dumps({"run_1": "failed"}), encoding="utf-8")

            runs = dataset_importer.apply_status_overlay([run], overlay_path)

        self.assertEqual(runs[0].status, "failed")

    def test_invalid_trajectory_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dataset_importer.normalize_trajectory(raw_row(), trajectory_id="../bad")

    def test_image_paths_null_mapping_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "hf_dataset"
            source_dir.mkdir()
            original_loader = dataset_importer._load_dataset_from_disk
            dataset_importer._load_dataset_from_disk = lambda path: [
                raw_row(sample_id=f"run_{i}") for i in range(5)
            ]
            try:
                runs = dataset_importer.import_sample(source_dir)
                assets = {
                    run.trajectory_id: dataset_importer.get_imported_screenshot_assets(run.trajectory_id)
                    for run in runs
                }
            finally:
                dataset_importer._load_dataset_from_disk = original_loader

        self.assertEqual(len(runs), 5)
        self.assertEqual([run.trajectory_id for run in runs], [f"run_{i}" for i in range(5)])
        for trajectory_id in [f"run_{i}" for i in range(5)]:
            self.assertEqual(assets[trajectory_id]["screenshot_001.png"], b"first")
            self.assertEqual(assets[trajectory_id]["screenshot_002.png"], b"second")


if __name__ == "__main__":
    unittest.main()
