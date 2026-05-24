"""ChromaDB persistence and retrieval for Trajecta's v1 RAG collections.

Three collections live under ``TRAJECTA_CHROMA_DIR`` (default
``data/chroma/``):

- ``failure_memory`` — reusable failure patterns; read-only at runtime,
  hydrated from ``data/failure_memory/cases.jsonl`` on startup.
- ``eval_cases`` — human-validated regression cases; written synchronously
  by ``POST /api/eval-cases``.
- ``successful_runs`` — imported runs with post-overlay ``status=="success"``;
  written synchronously by the import handler.

The embedding text formulas and metadata schemas are byte-exact
reproductions of ``docs/contracts.md`` "RAG Collection Contracts". Do not
normalize / lowercase / strip the embed text.

Embedding function selection (probe-based, mirrors ``llm.get_vlm_client``):

1. ``TRAJECTA_USE_FAKE_EMBEDDING=1`` → deterministic ``FakeEmbeddingFunction``
   (fast, network-free; used by the test suite).
2. ``TRAJECTA_EMBEDDING_MODEL`` set AND ``OPENAI_API_KEY`` set AND ``openai``
   importable → OpenAI embedding function with that model.
3. Otherwise → chromadb's default (sentence-transformers) embedding function.

Similarity scores / distances are stripped from every return path; v1 API
surface does not expose them.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.utils import embedding_functions

from backend.app import storage
from backend.app.schemas import EvalCase, FailureMemoryCase, TrajectoryRun


FAILURE_MEMORY_COLLECTION = "failure_memory"
EVAL_CASES_COLLECTION = "eval_cases"
SUCCESSFUL_RUNS_COLLECTION = "successful_runs"

_FAKE_EMBEDDING_DIM = 384

_client_cache: tuple[Path, ClientAPI] | None = None
_embedding_cache: tuple[tuple[bool, str | None, bool], EmbeddingFunction] | None = None


def _chroma_dir() -> Path:
    override = os.environ.get("TRAJECTA_CHROMA_DIR")
    if override:
        return Path(override).resolve()
    return (storage.data_dir() / "chroma").resolve()


def get_chroma_client() -> ClientAPI:
    """Return a ``PersistentClient`` rooted at ``TRAJECTA_CHROMA_DIR``.

    Cached per resolved persist directory so per-test ``tmpdir`` overrides
    take effect without reconstructing the client on every call.
    """

    global _client_cache
    target = _chroma_dir()
    if _client_cache is not None and _client_cache[0] == target:
        return _client_cache[1]
    target.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(target))
    _client_cache = (target, client)
    return client


class FakeEmbeddingFunction(EmbeddingFunction):
    """Deterministic, hash-based embedding function for tests.

    Each input text → 384-dim L2-normalized float vector derived from
    sha256 expansion. Identical text always produces the identical vector,
    so exact-match retrieval works; semantic ranking is meaningless but
    the delegation / wiring tests do not depend on it.
    """

    def __init__(self) -> None:
        pass

    @staticmethod
    def name() -> str:
        return "trajecta_fake_embedding"

    def get_config(self) -> dict:
        return {}

    @classmethod
    def build_from_config(cls, config: dict) -> "FakeEmbeddingFunction":
        return cls()

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 (chroma API)
        return [self._vec(text) for text in input]

    @staticmethod
    def _vec(text: str) -> list[float]:
        needed_bytes = _FAKE_EMBEDDING_DIM * 4
        buf = bytearray()
        counter = 0
        seed = text.encode("utf-8")
        while len(buf) < needed_bytes:
            buf.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
            counter += 1
        raw = bytes(buf[:needed_bytes])
        ints = struct.unpack(f">{_FAKE_EMBEDDING_DIM}I", raw)
        scale = float(1 << 32)
        vec = [v / scale for v in ints]
        norm = sum(x * x for x in vec) ** 0.5
        if norm == 0.0:
            return vec
        return [x / norm for x in vec]


def get_embedding_function() -> EmbeddingFunction:
    """Return the active embedding function per env configuration.

    Cached at module scope keyed on the resolved env-var tuple so the
    relatively expensive ``DefaultEmbeddingFunction`` (sentence-transformers
    + ONNX runtime) is not reconstructed on every collection access.
    Tests that flip env vars see the change because the cache key changes.
    """

    global _embedding_cache

    fake_flag = os.environ.get("TRAJECTA_USE_FAKE_EMBEDDING") == "1"
    model_name = os.environ.get("TRAJECTA_EMBEDDING_MODEL")
    api_key_present = bool(os.environ.get("OPENAI_API_KEY"))
    key = (fake_flag, model_name, api_key_present)
    if _embedding_cache is not None and _embedding_cache[0] == key:
        return _embedding_cache[1]

    if fake_flag:
        ef: EmbeddingFunction = FakeEmbeddingFunction()
    elif model_name and api_key_present:
        try:
            import openai  # noqa: F401  probe availability
        except ImportError:
            ef = embedding_functions.DefaultEmbeddingFunction()
        else:
            ef = embedding_functions.OpenAIEmbeddingFunction(
                api_key=os.environ["OPENAI_API_KEY"],
                model_name=model_name,
            )
    else:
        ef = embedding_functions.DefaultEmbeddingFunction()

    _embedding_cache = (key, ef)
    return ef


def _get_or_create(name: str) -> chromadb.Collection:
    return get_chroma_client().get_or_create_collection(
        name=name,
        embedding_function=get_embedding_function(),
    )


def failure_memory_collection() -> chromadb.Collection:
    return _get_or_create(FAILURE_MEMORY_COLLECTION)


def eval_cases_collection() -> chromadb.Collection:
    return _get_or_create(EVAL_CASES_COLLECTION)


def successful_runs_collection() -> chromadb.Collection:
    return _get_or_create(SUCCESSFUL_RUNS_COLLECTION)


def _embed_text_failure_memory(case: FailureMemoryCase) -> str:
    return " ".join(
        [
            case.failure_type,
            case.summary,
            case.fix_hint or "",
            " ".join(case.tags),
        ]
    ).strip()


def _embed_text_eval_case(case: EvalCase) -> str:
    # Success cases have all five failure-fields == None; treat them as
    # empty strings rather than crash. Evidence claims still contribute
    # signal so similarity search remains useful.
    parts = [
        case.task,
        case.failure_type or "",
        case.expected_behavior or "",
        case.actual_behavior or "",
        " ".join(e.claim for e in case.evidence),
        case.regression_rule or "",
    ]
    return " ".join(parts).strip()


def _embed_text_successful_run(run: TrajectoryRun) -> str:
    return run.task


def _failure_memory_metadata(case: FailureMemoryCase) -> dict[str, str | int | float | bool]:
    return {
        "case_id": case.case_id,
        "failure_type": case.failure_type,
        "summary": case.summary,
        "fix_hint": case.fix_hint or "",
        "source_run_id": case.source_run_id or "",
        "tags_json": json.dumps(case.tags),
    }


def _failure_memory_from_metadata(meta: dict) -> FailureMemoryCase:
    return FailureMemoryCase(
        case_id=meta["case_id"],
        failure_type=meta["failure_type"],
        summary=meta["summary"],
        fix_hint=meta["fix_hint"] or None,
        source_run_id=meta["source_run_id"] or None,
        tags=json.loads(meta.get("tags_json") or "[]"),
    )


def _eval_case_metadata(case: EvalCase) -> dict[str, str | int | float | bool]:
    # Chroma metadata rejects None values; coerce success-case Nones to
    # sentinel scalars. The authoritative shape lives in payload_json.
    return {
        "case_id": case.case_id,
        "source_run_id": case.source_run_id,
        "task": case.task,
        "failure_step": case.failure_step if case.failure_step is not None else -1,
        "failure_type": case.failure_type or "",
        "human_validated": case.human_validated,
        "is_success": case.is_success,
        "payload_json": case.model_dump_json(),
    }


def _successful_run_metadata(run: TrajectoryRun) -> dict[str, str | int | float | bool]:
    return {
        "run_id": run.run_id,
        "task": run.task,
        "status": run.status,
        "step_count": len(run.steps),
    }


def upsert_failure_memory(case: FailureMemoryCase) -> None:
    failure_memory_collection().upsert(
        ids=[case.case_id],
        documents=[_embed_text_failure_memory(case)],
        metadatas=[_failure_memory_metadata(case)],
    )


def upsert_eval_case(case: EvalCase) -> None:
    if not case.human_validated:
        raise ValueError(
            f"refuse to upsert draft eval case {case.case_id!r}; "
            "only human_validated=True cases are indexed"
        )
    eval_cases_collection().upsert(
        ids=[case.case_id],
        documents=[_embed_text_eval_case(case)],
        metadatas=[_eval_case_metadata(case)],
    )


def upsert_successful_run(run: TrajectoryRun) -> None:
    if run.status != "success":
        raise ValueError(
            f"refuse to upsert non-success run {run.run_id!r} "
            f"(status={run.status!r}); only success runs are indexed"
        )
    successful_runs_collection().upsert(
        ids=[run.run_id],
        documents=[_embed_text_successful_run(run)],
        metadatas=[_successful_run_metadata(run)],
    )


def delete_successful_run(run_id: str) -> None:
    successful_runs_collection().delete(ids=[run_id])


def query_failure_memory(query: str, top_k: int = 3) -> list[FailureMemoryCase]:
    if top_k <= 0:
        return []
    result = failure_memory_collection().query(
        query_texts=[query],
        n_results=top_k,
    )
    metas = (result.get("metadatas") or [[]])[0]
    return [_failure_memory_from_metadata(meta) for meta in metas]


def query_eval_cases(
    query: str, top_k: int = 3, only_validated: bool = True
) -> list[EvalCase]:
    if top_k <= 0:
        return []
    kwargs: dict = {"query_texts": [query], "n_results": top_k}
    if only_validated:
        kwargs["where"] = {"human_validated": True}
    result = eval_cases_collection().query(**kwargs)
    metas = (result.get("metadatas") or [[]])[0]
    cases = [EvalCase.model_validate_json(meta["payload_json"]) for meta in metas]
    if only_validated:
        cases = [case for case in cases if case.human_validated]
    return cases


def query_similar_successful_runs(
    task: str, top_k: int = 3, exclude_run_id: str | None = None
) -> list[dict]:
    if top_k <= 0:
        return []
    n_fetch = top_k + 1 if exclude_run_id else top_k
    result = successful_runs_collection().query(
        query_texts=[task],
        n_results=n_fetch,
    )
    metas = (result.get("metadatas") or [[]])[0]
    out: list[dict] = []
    for meta in metas:
        if exclude_run_id and meta.get("run_id") == exclude_run_id:
            continue
        out.append(
            {
                "run_id": meta["run_id"],
                "task": meta["task"],
                "status": meta["status"],
                "step_count": meta["step_count"],
            }
        )
        if len(out) >= top_k:
            break
    return out


def hydrate_all() -> None:
    """Idempotently re-upsert all on-disk records into their collections.

    Called on FastAPI startup. Re-invocation overwrites the same IDs, so
    repeated startups are safe — counts stay stable.
    """

    for case in storage.load_failure_memory():
        upsert_failure_memory(case)
    for case in storage.load_eval_cases():
        upsert_eval_case(case)
    for run in storage.list_runs():
        if run.status == "success":
            upsert_successful_run(run)
