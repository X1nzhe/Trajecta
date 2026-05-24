from __future__ import annotations

import os
import unittest
from pathlib import Path

from backend.app.llm import MockVLMClient, RealVLMClient, get_vlm_client


class LLMFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_key = os.environ.pop("OPENAI_API_KEY", None)
        self._saved_model = os.environ.pop("TRAJECTA_VLM_MODEL", None)

    def tearDown(self) -> None:
        if self._saved_key is not None:
            os.environ["OPENAI_API_KEY"] = self._saved_key
        else:
            os.environ.pop("OPENAI_API_KEY", None)
        if self._saved_model is not None:
            os.environ["TRAJECTA_VLM_MODEL"] = self._saved_model
        else:
            os.environ.pop("TRAJECTA_VLM_MODEL", None)

    def test_factory_returns_mock_when_no_api_key(self) -> None:
        client = get_vlm_client()
        self.assertIsInstance(client, MockVLMClient)
        self.assertEqual(client.model_name, "mock")

        summary = client.summarize_low_detail(
            Path("screenshot_001.png"), action_type="click", step_index=0
        )
        self.assertIsNotNone(summary)
        self.assertLessEqual(len(summary), 200)
        self.assertNotIn("\n", summary)

    def test_factory_returns_mock_when_only_api_key_set(self) -> None:
        os.environ["OPENAI_API_KEY"] = "test-key"
        self.assertIsInstance(get_vlm_client(), MockVLMClient)

    def test_factory_returns_mock_when_only_model_set(self) -> None:
        os.environ["TRAJECTA_VLM_MODEL"] = "gpt-4o-mini"
        self.assertIsInstance(get_vlm_client(), MockVLMClient)

    def test_factory_returns_real_client_when_both_env_set(self) -> None:
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["TRAJECTA_VLM_MODEL"] = "gpt-4o-mini"
        client = get_vlm_client()
        self.assertIsInstance(client, RealVLMClient)
        self.assertEqual(client.model_name, "gpt-4o-mini")


class MockVLMTests(unittest.TestCase):
    def test_output_is_byte_stable(self) -> None:
        client = MockVLMClient()
        first = client.summarize_low_detail(
            Path("dir/shot.png"), action_type="click", step_index=3
        )
        second = client.summarize_low_detail(
            Path("dir/shot.png"), action_type="click", step_index=3
        )
        self.assertEqual(first, second)

    def test_output_varies_by_inputs(self) -> None:
        client = MockVLMClient()
        a = client.summarize_low_detail(Path("a.png"), action_type="click", step_index=0)
        b = client.summarize_low_detail(Path("b.png"), action_type="click", step_index=0)
        c = client.summarize_low_detail(Path("a.png"), action_type="type", step_index=0)
        d = client.summarize_low_detail(Path("a.png"), action_type="click", step_index=1)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)

    def test_output_has_no_quoted_text_or_labels(self) -> None:
        client = MockVLMClient()
        summary = client.summarize_low_detail(
            Path("x.png"), action_type="click", step_index=0
        )
        self.assertNotIn('"', summary)
        self.assertNotIn("'", summary)
        self.assertIn("page=", summary)
        self.assertIn("focus=", summary)

    def test_output_is_single_line_and_bounded(self) -> None:
        client = MockVLMClient()
        summary = client.summarize_low_detail(
            Path("x.png"), action_type="click", step_index=0
        )
        self.assertLessEqual(len(summary), 200)
        self.assertNotIn("\n", summary)
        self.assertNotIn("\r", summary)


if __name__ == "__main__":
    unittest.main()
