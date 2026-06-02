"""ChromaDB persistence and retrieval for Trajecta's v1 RAG collections.

Three collections live under ``TRAJECTA_CHROMA_DIR`` (default
``data/chroma/``):

- ``failure_memory`` — reusable failure patterns (concept:
  ``failure_pattern_memory``); read-only at runtime, hydrated from
  ``data/failure_memory/cases.jsonl`` on startup.
- ``failure_eval_cases`` — failure-shaped, human-validated EvalCases used as
  failure precedents; written synchronously by ``POST /api/eval-cases``.
  Success-shaped EvalCases do not belong here.
- ``successful_trajectories`` — trajectories whose success was human-validated
  via a success-shaped ``EvalCase``; used by ``find_similar_successful_trajectory``
  for replay-and-diff. Starts empty after import and grows on validation.

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
from chromadb.utils.embedding_functions import register_embedding_function

from backend.app import storage
from backend.app.schemas import EvalCase, FailureMemoryCase, Trajectory


FAILURE_MEMORY_COLLECTION = "failure_memory"
FAILURE_EVAL_CASES_COLLECTION = "failure_eval_cases"
SUCCESSFUL_TRAJECTORIES_COLLECTION = "successful_trajectories"

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


@register_embedding_function
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


def _collection_names() -> set[str]:
    names: set[str] = set()
    for collection in get_chroma_client().list_collections():
        name = getattr(collection, "name", None)
        if isinstance(name, str):
            names.add(name)
        elif isinstance(collection, str):
            names.add(collection)
    return names


def reset_failure_memory_collection() -> None:
    """Drop the Chroma failure-memory collection before rebuilding from disk.

    SQLite rows are already resynced from ``cases.jsonl`` by
    ``storage.load_failure_memory``. Chroma needs the same reset because upsert
    alone cannot remove cases deleted or renamed in the JSONL source.
    """

    if FAILURE_MEMORY_COLLECTION in _collection_names():
        get_chroma_client().delete_collection(FAILURE_MEMORY_COLLECTION)


def failure_memory_collection() -> chromadb.Collection:
    return _get_or_create(FAILURE_MEMORY_COLLECTION)


def failure_eval_cases_collection() -> chromadb.Collection:
    return _get_or_create(FAILURE_EVAL_CASES_COLLECTION)


def successful_trajectories_collection() -> chromadb.Collection:
    return _get_or_create(SUCCESSFUL_TRAJECTORIES_COLLECTION)


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


def _embed_text_successful_trajectory(trajectory: Trajectory) -> str:
    return trajectory.task


def _failure_memory_metadata(case: FailureMemoryCase) -> dict[str, str | int | float | bool]:
    return {
        "case_id": case.case_id,
        "failure_type": case.failure_type,
        "summary": case.summary,
        "fix_hint": case.fix_hint or "",
        "source_trajectory_id": case.source_trajectory_id or "",
        "tags_json": json.dumps(case.tags),
    }


def _failure_memory_from_metadata(meta: dict) -> FailureMemoryCase:
    return FailureMemoryCase(
        case_id=meta["case_id"],
        failure_type=meta["failure_type"],
        summary=meta["summary"],
        fix_hint=meta["fix_hint"] or None,
        source_trajectory_id=meta["source_trajectory_id"] or None,
        tags=json.loads(meta.get("tags_json") or "[]"),
    )


def _eval_case_metadata(case: EvalCase) -> dict[str, str | int | float | bool]:
    # Chroma metadata rejects None values; coerce success-case Nones to
    # sentinel scalars. The authoritative shape lives in payload_json.
    return {
        "case_id": case.case_id,
        "source_trajectory_id": case.source_trajectory_id,
        "task": case.task,
        "failure_step": case.failure_step if case.failure_step is not None else -1,
        "failure_type": case.failure_type or "",
        "human_validated": case.human_validated,
        "is_success": case.is_success,
        "payload_json": case.model_dump_json(),
    }


def _successful_trajectory_metadata(trajectory: Trajectory) -> dict[str, str | int | float | bool]:
    return {
        "trajectory_id": trajectory.trajectory_id,
        "task": trajectory.task,
        "status": trajectory.status,
        "step_count": len(trajectory.steps),
    }


def upsert_failure_memory(case: FailureMemoryCase) -> None:
    failure_memory_collection().upsert(
        ids=[case.case_id],
        documents=[_embed_text_failure_memory(case)],
        metadatas=[_failure_memory_metadata(case)],
    )


def upsert_failure_eval_case(case: EvalCase) -> None:
    if not case.human_validated:
        raise ValueError(
            f"refuse to upsert draft eval case {case.case_id!r}; "
            "only human_validated=True cases are indexed"
        )
    if case.is_success:
        raise ValueError(
            f"refuse to upsert success-shaped eval case {case.case_id!r} into "
            "failure_eval_cases; success trajectories belong in "
            "successful_trajectories"
        )
    failure_eval_cases_collection().upsert(
        ids=[case.case_id],
        documents=[_embed_text_eval_case(case)],
        metadatas=[_eval_case_metadata(case)],
    )


def upsert_successful_trajectory(trajectory: Trajectory) -> None:
    if trajectory.status != "success":
        raise ValueError(
            f"refuse to upsert non-success trajectory {trajectory.trajectory_id!r} "
            f"(status={trajectory.status!r}); only success runs are indexed"
        )
    successful_trajectories_collection().upsert(
        ids=[trajectory.trajectory_id],
        documents=[_embed_text_successful_trajectory(trajectory)],
        metadatas=[_successful_trajectory_metadata(trajectory)],
    )


def delete_successful_trajectory(trajectory_id: str) -> None:
    successful_trajectories_collection().delete(ids=[trajectory_id])


def query_failure_memory(
    query: str,
    top_k: int = 3,
    exclude_source_trajectory_id: str | None = None,
) -> list[FailureMemoryCase]:
    """Retrieve up to ``top_k`` failure-memory cases similar to ``query``.

    ``exclude_source_trajectory_id`` filters out any case whose ``source_trajectory_id`` matches
    the given value. This guards against retrieval leakage when the agent analyzes
    a trajectory that is itself the source of an existing failure-memory case (e.g. seed
    cases in ``data/failure_memory/cases.jsonl`` whose ``source_trajectory_id`` is one of
    the runs in the golden eval set). Mirror of ``exclude_trajectory_id`` in
    ``query_similar_successful_trajectories``.
    """

    if top_k <= 0:
        return []
    kwargs: dict = {"query_texts": [query], "n_results": top_k}
    if exclude_source_trajectory_id:
        kwargs["where"] = {"source_trajectory_id": {"$ne": exclude_source_trajectory_id}}
    result = failure_memory_collection().query(**kwargs)
    metas = (result.get("metadatas") or [[]])[0]
    return [_failure_memory_from_metadata(meta) for meta in metas]


def query_failure_eval_cases(
    query: str,
    top_k: int = 3,
    only_validated: bool = True,
    exclude_source_trajectory_id: str | None = None,
) -> list[EvalCase]:
    """Retrieve up to ``top_k`` failure eval cases similar to ``query``.

    ``exclude_source_trajectory_id`` filters out any case whose ``source_trajectory_id``
    matches the given value. Mirrors the leakage guard on
    ``query_failure_memory``: when the agent re-analyzes trajectory X, an
    EvalCase derived from X (whose verdict literally IS the answer for X)
    must not surface here.
    """
    if top_k <= 0:
        return []
    kwargs: dict = {"query_texts": [query], "n_results": top_k}
    conditions: list[dict] = []
    if only_validated:
        conditions.append({"human_validated": True})
    if exclude_source_trajectory_id:
        conditions.append({"source_trajectory_id": {"$ne": exclude_source_trajectory_id}})
    if len(conditions) == 1:
        kwargs["where"] = conditions[0]
    elif len(conditions) > 1:
        kwargs["where"] = {"$and": conditions}
    result = failure_eval_cases_collection().query(**kwargs)
    metas = (result.get("metadatas") or [[]])[0]
    cases = [EvalCase.model_validate_json(meta["payload_json"]) for meta in metas]
    if only_validated:
        cases = [case for case in cases if case.human_validated]
    if exclude_source_trajectory_id:
        cases = [case for case in cases if case.source_trajectory_id != exclude_source_trajectory_id]
    return cases


def query_similar_successful_trajectories(
    task: str, top_k: int = 3, exclude_trajectory_id: str | None = None
) -> list[dict]:
    if top_k <= 0:
        return []
    n_fetch = top_k + 1 if exclude_trajectory_id else top_k
    result = successful_trajectories_collection().query(
        query_texts=[task],
        n_results=n_fetch,
    )
    metas = (result.get("metadatas") or [[]])[0]
    out: list[dict] = []
    for meta in metas:
        if exclude_trajectory_id and meta.get("trajectory_id") == exclude_trajectory_id:
            continue
        out.append(
            {
                "trajectory_id": meta["trajectory_id"],
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

    Called on FastAPI startup. Failure memory is fully rebuilt so edits to
    ``data/failure_memory/cases.jsonl`` cannot leave stale vectors behind;
    failure eval cases (failure-shaped only) and successful trajectories are
    still upserted by stable IDs.
    """

    reset_failure_memory_collection()
    for case in storage.load_failure_memory():
        upsert_failure_memory(case)
    for case in storage.load_eval_cases():
        # Only failure-shaped EvalCases are failure precedents. Success-shaped
        # cases are represented by their trajectory in successful_trajectories
        # (rebuilt below from trajectory.status), so they must not leak in here.
        if not case.is_success:
            upsert_failure_eval_case(case)
    for trajectory in storage.list_trajectories():
        if trajectory.status == "success":
            upsert_successful_trajectory(trajectory)
