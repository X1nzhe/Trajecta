"""Pytest configuration shared by the whole backend suite.

The default chromadb embedding function downloads a ~80 MB
sentence-transformers model on first use. That would blow the
sub-30-second cold-cache test budget for the full suite. Force the
deterministic ``FakeEmbeddingFunction`` for every test so chromadb
collections are still exercised end-to-end without any model download.

The opt-in integration test in ``test_rag.py`` clears this variable via
``monkeypatch`` so it can exercise the real default embedder when run with
``TRAJECTA_RAG_INTEGRATION=1``.
"""

from __future__ import annotations

import os


os.environ.setdefault("TRAJECTA_USE_FAKE_EMBEDDING", "1")
