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

import pytest


os.environ.setdefault("TRAJECTA_USE_FAKE_EMBEDDING", "1")


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from backend.app import db

    tmp = tempfile.TemporaryDirectory()
    monkeypatch.setenv("TRAJECTA_DATA_DIR", tmp.name)
    db.reset_engine_cache()
    try:
        yield tmp.name
    finally:
        db.reset_engine_cache()
        tmp.cleanup()
