from __future__ import annotations

import io
import json
import os
import unittest

from backend.app import preprocess, storage
from backend.app.llm import MockVLMClient
from backend.app.schemas import (
    Coordinate,
    StepAction,
    StepObservation,
    StepResult,
    TrajectoryDigest,
    TrajectoryRun,
    TrajectoryStep,
)


def _make_step(
    *,
    index: int,
    screenshot: str | None = "screenshot_001.png",
    action: StepAction | None = None,
    visible_text: str | None = None,
    url: str | None = None,
    title: str | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    result_status: str = "unknown",
) -> TrajectoryStep:
    return TrajectoryStep(
        index=index,
        observation=StepObservation(
            screenshot=screenshot,
            url=url,
            title=title,
            visible_text=visible_text,
        ),
        action=action or StepAction(type="wait", raw="wait()"),
        result=StepResult(status=result_status),
        metadata={
            "image_width": image_width,
            "image_height": image_height,
        },
    )


def _make_run(
    *,
    run_id: str = "run_pre_1",
    steps: list[TrajectoryStep] | None = None,
) -> TrajectoryRun:
    return TrajectoryRun(
        run_id=run_id,
        task="Find a result",
        status="failed",
        steps=steps or [_make_step(index=0)],
    )


def _png_bytes(*, width: int = 64, height: int = 48) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _attach_screenshot(run_id: str, filename: str, *, width: int = 64, height: int = 48) -> None:
    storage.save_screenshots(run_id, {filename: _png_bytes(width=width, height=height)})


class SpyVLMClient:
    def __init__(self, *, model_name: str = "spy") -> None:
        self.model_name = model_name
        self.calls: list[tuple[str, str, int]] = []

    def summarize_low_detail(self, image_bytes, *, image_name, action_type, step_index):
        del image_bytes
        self.calls.append((image_name, action_type, step_index))
        return f"spy summary for {image_name} {action_type} {step_index}"


class PreprocessTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_api_key = os.environ.pop("OPENAI_API_KEY", None)
        self.previous_model = os.environ.pop("TRAJECTA_VLM_MODEL", None)

    def tearDown(self) -> None:
        if self.previous_api_key is not None:
            os.environ["OPENAI_API_KEY"] = self.previous_api_key
        if self.previous_model is not None:
            os.environ["TRAJECTA_VLM_MODEL"] = self.previous_model


class BuildDigestTests(PreprocessTestBase):
    def test_one_stepdigest_per_step(self) -> None:
        run = _make_run(
            steps=[
                _make_step(index=0, screenshot="a.png"),
                _make_step(index=1, screenshot="b.png"),
                _make_step(index=2, screenshot="c.png"),
            ]
        )
        digest = preprocess.build_digest(run, client=MockVLMClient())

        self.assertEqual(digest.step_count, 3)
        self.assertEqual([s.index for s in digest.steps], [0, 1, 2])
        self.assertEqual(digest.run_id, run.run_id)
        self.assertEqual(digest.task, run.task)
        self.assertEqual(digest.preprocess_model, "mock")
        self.assertEqual(digest.preprocess_version, "v1")

    def test_validates_schema(self) -> None:
        run = _make_run()
        digest = preprocess.build_digest(run, client=MockVLMClient())
        TrajectoryDigest.model_validate(digest.model_dump(mode="json"))

    def test_raises_on_empty_steps(self) -> None:
        run = TrajectoryRun(run_id="empty", task="t", steps=[])
        with self.assertRaises(ValueError):
            preprocess.build_digest(run, client=MockVLMClient())

    def test_has_screenshot_reflects_disk_state(self) -> None:
        run = _make_run(
            steps=[
                _make_step(index=0, screenshot="present.png"),
                _make_step(index=1, screenshot="missing.png"),
                _make_step(index=2, screenshot=None),
            ]
        )
        storage.save_run(run)
        _attach_screenshot(run.run_id, "present.png")

        digest = preprocess.build_digest(run, client=MockVLMClient())

        self.assertTrue(digest.steps[0].has_screenshot)
        self.assertFalse(digest.steps[1].has_screenshot)
        self.assertFalse(digest.steps[2].has_screenshot)

    def test_coord_validation_uses_image_bytes_fallback(self) -> None:
        run = _make_run(
            steps=[
                _make_step(
                    index=0,
                    screenshot="step0.png",
                    action=StepAction(
                        type="click",
                        coordinates=Coordinate(x=10, y=10),
                        raw="click(10,10)",
                    ),
                    image_width=None,
                    image_height=None,
                ),
                _make_step(
                    index=1,
                    screenshot="step1.png",
                    action=StepAction(
                        type="click",
                        coordinates=Coordinate(x=999, y=999),
                        raw="click(999,999)",
                    ),
                    image_width=None,
                    image_height=None,
                ),
            ]
        )
        storage.save_run(run)
        _attach_screenshot(run.run_id, "step0.png", width=64, height=48)
        _attach_screenshot(run.run_id, "step1.png", width=64, height=48)

        digest = preprocess.build_digest(run, client=MockVLMClient())

        self.assertEqual(digest.steps[0].coord_validation_status, "validated")
        self.assertEqual(digest.steps[1].coord_validation_status, "out_of_bounds")
        self.assertNotEqual(digest.steps[0].coord_validation_status, "unknown")

    def test_skip_vlm_when_visible_text_present(self) -> None:
        spy = SpyVLMClient()
        run = _make_run(
            steps=[
                _make_step(index=0, screenshot="a.png", visible_text="Submit | Cancel"),
                _make_step(index=1, screenshot="b.png"),
            ]
        )
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")
        _attach_screenshot(run.run_id, "b.png")

        digest = preprocess.build_digest(run, client=spy)

        self.assertIsNone(digest.steps[0].vlm_low_detail_summary)
        self.assertIsNotNone(digest.steps[1].vlm_low_detail_summary)
        self.assertEqual([call[2] for call in spy.calls], [1])

    def test_title_and_url_alone_do_not_skip_vlm(self) -> None:
        """Title + URL are page metadata, not DOM/accessibility text.

        Per docs/preprocessing.md, the VLM is only skipped when the source
        dataset provides actual page text (visible_text). Knowing the page
        title and URL does not tell the agent what is on the page, so the
        low-detail orientation hint is still required.
        """

        spy = SpyVLMClient()
        run = _make_run(
            steps=[
                _make_step(
                    index=0,
                    screenshot="a.png",
                    title="Search Results",
                    url="https://example.com/search?q=foo",
                ),
            ]
        )
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")

        digest = preprocess.build_digest(run, client=spy)

        self.assertIsNotNone(digest.steps[0].vlm_low_detail_summary)
        self.assertEqual(len(spy.calls), 1)

    def test_skip_vlm_when_screenshot_missing(self) -> None:
        spy = SpyVLMClient()
        run = _make_run(
            steps=[_make_step(index=0, screenshot="missing.png")],
        )
        storage.save_run(run)

        digest = preprocess.build_digest(run, client=spy)

        self.assertIsNone(digest.steps[0].vlm_low_detail_summary)
        self.assertEqual(spy.calls, [])

    def test_mock_vlm_is_byte_stable(self) -> None:
        run = _make_run(
            steps=[
                _make_step(index=0, screenshot="a.png"),
                _make_step(index=1, screenshot="b.png"),
            ]
        )
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")
        _attach_screenshot(run.run_id, "b.png")

        first = preprocess.build_digest(run, client=MockVLMClient())
        second = preprocess.build_digest(run, client=MockVLMClient())

        first_json = json.dumps(first.model_dump(mode="json"), sort_keys=True)
        second_json = json.dumps(second.model_dump(mode="json"), sort_keys=True)
        self.assertEqual(first_json, second_json)


class LoadOrBuildDigestTests(PreprocessTestBase):
    def test_cache_hit_avoids_vlm(self) -> None:
        run = _make_run(steps=[_make_step(index=0, screenshot="a.png")])
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")

        cached = preprocess.build_digest(run, client=MockVLMClient())
        storage.save_digest(run.run_id, cached)

        spy = SpyVLMClient(model_name="mock")
        import backend.app.preprocess as preprocess_mod

        original_factory = preprocess_mod.get_vlm_client
        preprocess_mod.get_vlm_client = lambda: spy
        try:
            result = preprocess.load_or_build_digest(run.run_id)
        finally:
            preprocess_mod.get_vlm_client = original_factory

        self.assertEqual(result.model_dump(mode="json"), cached.model_dump(mode="json"))
        self.assertEqual(spy.calls, [])

    def test_rebuilds_on_model_mismatch(self) -> None:
        run = _make_run(steps=[_make_step(index=0, screenshot="a.png")])
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")

        stale = preprocess.build_digest(run, client=MockVLMClient())
        stale = stale.model_copy(update={"preprocess_model": "old-model-id"})
        storage.save_digest(run.run_id, stale)

        result = preprocess.load_or_build_digest(run.run_id)

        self.assertEqual(result.preprocess_model, "mock")
        on_disk = storage.load_digest(run.run_id)
        self.assertIsNotNone(on_disk)
        self.assertEqual(on_disk.preprocess_model, "mock")

    def test_rebuilds_on_version_mismatch(self) -> None:
        run = _make_run(steps=[_make_step(index=0, screenshot="a.png")])
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")

        stale_payload = preprocess.build_digest(run, client=MockVLMClient()).model_dump(mode="json")
        stale_payload["preprocess_version"] = "v0"
        storage.save_digest(run.run_id, TrajectoryDigest.model_validate(stale_payload))

        result = preprocess.load_or_build_digest(run.run_id)

        self.assertEqual(result.preprocess_version, "v1")

    def test_builds_and_saves_when_cache_absent(self) -> None:
        run = _make_run(steps=[_make_step(index=0, screenshot="a.png")])
        storage.save_run(run)
        _attach_screenshot(run.run_id, "a.png")

        self.assertIsNone(storage.load_digest(run.run_id))
        result = preprocess.load_or_build_digest(run.run_id)

        self.assertEqual(result.run_id, run.run_id)
        self.assertIsNotNone(storage.load_digest(run.run_id))


if __name__ == "__main__":
    unittest.main()
