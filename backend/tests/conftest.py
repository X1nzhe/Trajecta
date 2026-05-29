"""Pytest configuration shared by the whole backend suite.

The default chromadb embedding function downloads a ~80 MB
sentence-transformers model on first use. That would blow the
sub-30-second cold-cache test budget for the full suite. Force the
deterministic ``FakeEmbeddingFunction`` for every test so chromadb
collections are still exercised end-to-end without any model download.

The opt-in integration test in ``test_rag.py`` clears this variable via
``monkeypatch`` so it can exercise the real default embedder when run with
``TRAJECTA_RAG_INTEGRATION=1``.

A second autouse fixture isolates the SQLite-backed storage per test:
each test gets its own ``TRAJECTA_DATA_DIR`` and the cached engine in
``backend.app.db`` is reset so the next ``storage.*`` call re-creates
the schema inside that fresh directory.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv


# Load .env from the repo root so opt-in integration tests (e.g.,
# test_real_llm_integration) see OPENAI_API_KEY + TRAJECTA_AGENT_MODEL
# without the user having to remember to `export` them. Existing shell
# exports still win (override=False is the default).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

os.environ.setdefault("TRAJECTA_USE_FAKE_EMBEDDING", "1")


# Capture real LLM env vars loaded from .env so individual opt-in tests can
# restore them when they explicitly want the real API. The autouse fixture
# below scrubs them per-test so the default suite never accidentally hits
# OpenAI (cost + flakiness + would also defeat MockVLMClient assertions).
_REAL_LLM_ENV = {
    key: os.environ.get(key)
    for key in ("OPENAI_API_KEY", "TRAJECTA_AGENT_MODEL", "TRAJECTA_VLM_MODEL")
}


@pytest.fixture
def real_llm_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Opt-in fixture: restore real LLM env vars captured at conftest load.

    Use in integration tests via ``def test_x(self, real_llm_env): ...``
    or via ``@pytest.mark.usefixtures("real_llm_env")``. The skipif guard
    on those tests should check the captured dict (``_REAL_LLM_ENV``) or
    use the helper below.
    """

    restored = {}
    for key, value in _REAL_LLM_ENV.items():
        if value is not None:
            monkeypatch.setenv(key, value)
            restored[key] = value
    return restored


def real_llm_configured() -> bool:
    """True when .env (or the shell) provided both API key and agent model."""

    return bool(_REAL_LLM_ENV.get("OPENAI_API_KEY") and _REAL_LLM_ENV.get("TRAJECTA_AGENT_MODEL"))


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from backend.app import db

    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("TRAJECTA_DATA_DIR", tmp.name)
    # Default every test to the no-key environment. Integration tests that
    # need the real API must opt back in via the ``real_llm_env`` fixture.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TRAJECTA_AGENT_MODEL", raising=False)
    monkeypatch.delenv("TRAJECTA_VLM_MODEL", raising=False)
    monkeypatch.delenv("TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION", raising=False)
    db.reset_engine_cache()
    try:
        yield tmp.name
    finally:
        db.reset_engine_cache()
        tmp.cleanup()
