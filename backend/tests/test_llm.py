from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest import mock

from backend.app.llm import (
    GEMINI_OPENAI_BASE_URL,
    MockVLMClient,
    RealVLMClient,
    get_vlm_client,
    resolve_model_provider,
)


_OPENAI_AVAILABLE = importlib.util.find_spec("openai") is not None
_FAKE_BYTES = b"\x89PNG\r\n\x1a\n"


class LLMFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {
            key: os.environ.pop(key, None)
            for key in (
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "GEMINI_API_KEY",
                "GEMINI_BASE_URL",
                "TRAJECTA_VLM_MODEL",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_factory_returns_mock_when_no_api_key(self) -> None:
        client = get_vlm_client()
        self.assertIsInstance(client, MockVLMClient)
        self.assertEqual(client.model_name, "mock")

        summary = client.summarize_low_detail(
            _FAKE_BYTES, image_name="screenshot_001.png", action_type="click", step_index=0
        )
        self.assertIsNotNone(summary)
        self.assertLessEqual(len(summary), 300)
        self.assertNotIn("\n", summary)

    def test_factory_returns_mock_when_only_api_key_set(self) -> None:
        os.environ["OPENAI_API_KEY"] = "test-key"
        self.assertIsInstance(get_vlm_client(), MockVLMClient)

    def test_factory_returns_mock_when_only_model_set(self) -> None:
        os.environ["TRAJECTA_VLM_MODEL"] = "gpt-4o-mini"
        self.assertIsInstance(get_vlm_client(), MockVLMClient)

    def test_resolver_routes_gemini_models_to_gemini_key_and_default_base_url(self) -> None:
        os.environ["OPENAI_API_KEY"] = "openai-key"
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        resolved = resolve_model_provider("gemini-3.1-flash-lite")
        self.assertEqual(resolved.provider, "gemini")
        self.assertEqual(resolved.api_key, "gemini-key")
        self.assertEqual(resolved.base_url, GEMINI_OPENAI_BASE_URL)

    def test_resolver_allows_gemini_base_url_override(self) -> None:
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        os.environ["GEMINI_BASE_URL"] = "https://gemini.example/openai/"
        resolved = resolve_model_provider("gemini-3.1-flash-lite")
        self.assertEqual(resolved.provider, "gemini")
        self.assertEqual(resolved.api_key, "gemini-key")
        self.assertEqual(resolved.base_url, "https://gemini.example/openai/")

    def test_resolver_routes_non_gemini_models_to_openai_key_and_base_url(self) -> None:
        os.environ["OPENAI_API_KEY"] = "openai-key"
        os.environ["OPENAI_BASE_URL"] = "https://openai-compatible.example/v1"
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        resolved = resolve_model_provider("gpt-4o-mini")
        self.assertEqual(resolved.provider, "openai")
        self.assertEqual(resolved.api_key, "openai-key")
        self.assertEqual(resolved.base_url, "https://openai-compatible.example/v1")

    @unittest.skipUnless(_OPENAI_AVAILABLE, "openai package not installed")
    def test_factory_returns_real_client_when_env_set_and_openai_installed(self) -> None:
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["TRAJECTA_VLM_MODEL"] = "gpt-4o-mini"
        client = get_vlm_client()
        self.assertIsInstance(client, RealVLMClient)
        self.assertEqual(client.model_name, "gpt-4o-mini")

    @unittest.skipUnless(_OPENAI_AVAILABLE, "openai package not installed")
    def test_factory_returns_real_client_for_gemini_model_with_gemini_key(self) -> None:
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        os.environ["TRAJECTA_VLM_MODEL"] = "gemini-3.1-flash-lite"
        client = get_vlm_client()
        self.assertIsInstance(client, RealVLMClient)
        self.assertEqual(client.model_name, "gemini-3.1-flash-lite")
        self.assertEqual(client._base_url, GEMINI_OPENAI_BASE_URL)

    @unittest.skipUnless(_OPENAI_AVAILABLE, "openai package not installed")
    def test_factory_uses_openai_base_url_for_non_gemini_model(self) -> None:
        os.environ["OPENAI_API_KEY"] = "openai-key"
        os.environ["OPENAI_BASE_URL"] = "https://openai-compatible.example/v1"
        os.environ["GEMINI_API_KEY"] = "gemini-key"
        os.environ["TRAJECTA_VLM_MODEL"] = "gpt-4o-mini"
        client = get_vlm_client()
        self.assertIsInstance(client, RealVLMClient)
        self.assertEqual(client.model_name, "gpt-4o-mini")
        self.assertEqual(client._base_url, "https://openai-compatible.example/v1")

    def test_factory_returns_mock_when_openai_not_importable(self) -> None:
        """Even with both env vars set, missing `openai` must fall back to Mock.

        Without this, a misconfigured environment silently writes a digest
        with ``preprocess_model="gpt-4o-mini"`` whose every step has
        ``vlm_low_detail_summary=None`` — looks like the real model ran but
        produced nothing.
        """

        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["TRAJECTA_VLM_MODEL"] = "gpt-4o-mini"
        # Force `from openai import OpenAI` to raise ImportError regardless
        # of whether the package is actually installed in this environment.
        with mock.patch.dict(sys.modules, {"openai": None}):
            client = get_vlm_client()
        self.assertIsInstance(client, MockVLMClient)


class MockVLMTests(unittest.TestCase):
    def test_output_is_byte_stable(self) -> None:
        client = MockVLMClient()
        first = client.summarize_low_detail(
            _FAKE_BYTES, image_name="dir/shot.png", action_type="click", step_index=3
        )
        second = client.summarize_low_detail(
            _FAKE_BYTES, image_name="dir/shot.png", action_type="click", step_index=3
        )
        self.assertEqual(first, second)

    def test_output_varies_by_inputs(self) -> None:
        client = MockVLMClient()
        a = client.summarize_low_detail(_FAKE_BYTES, image_name="a.png", action_type="click", step_index=0)
        b = client.summarize_low_detail(_FAKE_BYTES, image_name="b.png", action_type="click", step_index=0)
        c = client.summarize_low_detail(_FAKE_BYTES, image_name="a.png", action_type="type", step_index=0)
        d = client.summarize_low_detail(_FAKE_BYTES, image_name="a.png", action_type="click", step_index=1)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)

    def test_output_has_no_quoted_text_or_labels(self) -> None:
        client = MockVLMClient()
        summary = client.summarize_low_detail(
            _FAKE_BYTES, image_name="x.png", action_type="click", step_index=0
        )
        self.assertNotIn('"', summary)
        self.assertNotIn("'", summary)
        self.assertIn("page=", summary)
        self.assertIn("focus=", summary)

    def test_output_is_single_line_and_bounded(self) -> None:
        client = MockVLMClient()
        summary = client.summarize_low_detail(
            _FAKE_BYTES, image_name="x.png", action_type="click", step_index=0
        )
        self.assertLessEqual(len(summary), 300)
        self.assertNotIn("\n", summary)
        self.assertNotIn("\r", summary)


if __name__ == "__main__":
    unittest.main()
