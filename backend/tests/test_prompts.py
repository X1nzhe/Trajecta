"""Tests for the prompt registry helpers, including Phase 8 B6 Spotlighting."""

from __future__ import annotations

import pytest

from backend.app import prompts


class TestSpotlightToken:
    def test_new_token_has_8_lowercase_hex_chars(self) -> None:
        token = prompts.new_spotlight_token()
        assert len(token) == 8
        assert all(c in "0123456789abcdef" for c in token)

    def test_new_tokens_are_distinct(self) -> None:
        tokens = {prompts.new_spotlight_token() for _ in range(50)}
        # 8 hex chars = 32 bits of entropy; 50 draws colliding is ~1e-7
        assert len(tokens) == 50

    def test_set_and_get_roundtrip(self) -> None:
        token = prompts.new_spotlight_token()
        prompts.set_spotlight_token(token)
        assert prompts.current_spotlight_token() == token

    def test_set_token_rejects_wrong_shape(self) -> None:
        with pytest.raises(ValueError, match="invalid spotlight token"):
            prompts.set_spotlight_token("not-hex!!")
        with pytest.raises(ValueError, match="invalid spotlight token"):
            prompts.set_spotlight_token("aaaa")  # too short
        with pytest.raises(ValueError, match="invalid spotlight token"):
            prompts.set_spotlight_token("AABBCCDD")  # uppercase rejected

    def test_set_token_accepts_none_to_clear(self) -> None:
        prompts.set_spotlight_token(prompts.new_spotlight_token())
        prompts.set_spotlight_token(None)
        assert prompts.current_spotlight_token() is None


class TestSpotlightingEnabled:
    def test_default_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(prompts.SPOTLIGHTING_ENV_VAR, raising=False)
        assert prompts.spotlighting_enabled() is True

    @pytest.mark.parametrize("value", ["on", "ON", "true", "True", "1", "yes"])
    def test_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, value)
        assert prompts.spotlighting_enabled() is True

    @pytest.mark.parametrize("value", ["off", "OFF", "false", "False", "0", "no"])
    def test_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, value)
        assert prompts.spotlighting_enabled() is False

    def test_invalid_value_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "sometimes")
        with pytest.raises(ValueError, match="invalid TRAJECTA_SPOTLIGHTING"):
            prompts.spotlighting_enabled()


class TestSpotlightWrap:
    @pytest.fixture(autouse=True)
    def _on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # conftest defaults to off so other tests keep their fixture
        # assertions intact; override to on for this class so wrap behaviour
        # is exercised. Individual tests can set "off" to test that branch.
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "on")

    def test_wraps_with_active_token(self) -> None:
        token = prompts.new_spotlight_token()
        prompts.set_spotlight_token(token)
        wrapped = prompts.spotlight_wrap("hello world")
        assert wrapped == f"<TRAJECTA_DATA_{token}>hello world</TRAJECTA_DATA_{token}>"

    def test_wraps_consistently_within_one_token_context(self) -> None:
        token = prompts.new_spotlight_token()
        prompts.set_spotlight_token(token)
        first = prompts.spotlight_wrap("a")
        second = prompts.spotlight_wrap("b")
        assert f"<TRAJECTA_DATA_{token}>" in first
        assert f"<TRAJECTA_DATA_{token}>" in second
        # same token in both wraps
        assert first.count(token) == 2
        assert second.count(token) == 2

    def test_off_mode_returns_identity_without_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        # no set_spotlight_token call
        assert prompts.spotlight_wrap("plain") == "plain"

    def test_off_mode_returns_identity_even_with_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        prompts.set_spotlight_token(prompts.new_spotlight_token())
        assert prompts.spotlight_wrap("plain") == "plain"

    def test_on_mode_without_token_raises(self) -> None:
        # token defaulted to None by conftest autouse fixture
        with pytest.raises(RuntimeError, match="without an active token"):
            prompts.spotlight_wrap("oops")

    def test_non_string_input_raises_typeerror(self) -> None:
        prompts.set_spotlight_token(prompts.new_spotlight_token())
        with pytest.raises(TypeError, match="expected str"):
            prompts.spotlight_wrap(None)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="expected str"):
            prompts.spotlight_wrap(123)  # type: ignore[arg-type]

    def test_empty_string_is_wrapped(self) -> None:
        token = prompts.new_spotlight_token()
        prompts.set_spotlight_token(token)
        assert prompts.spotlight_wrap("") == f"<TRAJECTA_DATA_{token}></TRAJECTA_DATA_{token}>"


class TestSpotlightWrapOptional:
    @pytest.fixture(autouse=True)
    def _on_with_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "on")
        prompts.set_spotlight_token(prompts.new_spotlight_token())

    def test_wraps_non_empty_string(self) -> None:
        assert prompts.spotlight_wrap_optional("x").startswith("<TRAJECTA_DATA_")

    def test_passes_through_none(self) -> None:
        assert prompts.spotlight_wrap_optional(None) is None

    def test_passes_through_empty_string(self) -> None:
        assert prompts.spotlight_wrap_optional("") == ""

    def test_off_mode_is_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        assert prompts.spotlight_wrap_optional("plain") == "plain"
        assert prompts.spotlight_wrap_optional(None) is None


class TestPromptBundleSpotlightingPreamble:
    VERSION = "v5_constraint_verification"

    def test_on_prepends_preamble_to_system(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "on")
        prompts.load_prompt_bundle.cache_clear()
        bundle = prompts.load_prompt_bundle(self.VERSION)
        assert bundle.system.startswith(prompts.SPOTLIGHTING_PREAMBLE)

    def test_off_leaves_system_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        prompts.load_prompt_bundle.cache_clear()
        bundle = prompts.load_prompt_bundle(self.VERSION)
        assert not bundle.system.startswith(prompts.SPOTLIGHTING_PREAMBLE)
        # the underlying preamble text is not present anywhere in v5
        assert prompts.SPOTLIGHTING_PREAMBLE not in bundle.system

    def test_on_off_sha256_diverge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        prompts.load_prompt_bundle.cache_clear()
        off_bundle = prompts.load_prompt_bundle(self.VERSION)

        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "on")
        prompts.load_prompt_bundle.cache_clear()
        on_bundle = prompts.load_prompt_bundle(self.VERSION)

        assert off_bundle.system_sha256 != on_bundle.system_sha256
        assert off_bundle.sha256 != on_bundle.sha256
        # followup file bytes are unchanged → its sha must match
        assert off_bundle.followup_sha256 == on_bundle.followup_sha256

    def test_active_bundle_threads_env_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.PROMPT_ENV_VAR, self.VERSION)
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "on")
        prompts.load_prompt_bundle.cache_clear()
        bundle = prompts.active_prompt_bundle()
        assert bundle.system.startswith(prompts.SPOTLIGHTING_PREAMBLE)

    def test_explicit_spotlighting_param_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        prompts.load_prompt_bundle.cache_clear()
        on_bundle = prompts.load_prompt_bundle(self.VERSION, spotlighting=True)
        off_bundle = prompts.load_prompt_bundle(self.VERSION, spotlighting=False)
        assert on_bundle.system.startswith(prompts.SPOTLIGHTING_PREAMBLE)
        assert not off_bundle.system.startswith(prompts.SPOTLIGHTING_PREAMBLE)
        assert on_bundle.sha256 != off_bundle.sha256

    def test_combined_sha_includes_spotlighting_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even if (hypothetically) preamble bytes equalled the file delta in
        # some other version, the combined sha must still differ because it
        # hashes the "spotlighting=on|off" marker line.
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "off")
        prompts.load_prompt_bundle.cache_clear()
        off_combined = prompts.load_prompt_bundle(self.VERSION).sha256
        monkeypatch.setenv(prompts.SPOTLIGHTING_ENV_VAR, "on")
        prompts.load_prompt_bundle.cache_clear()
        on_combined = prompts.load_prompt_bundle(self.VERSION).sha256
        assert off_combined != on_combined
