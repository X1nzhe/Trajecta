"""SQLite engine + session management for Trajecta.

The database file lives at ``data_dir() / "trajecta.db"`` (so ``TRAJECTA_DATA_DIR``
still controls test isolation). The engine is cached per resolved path; flipping
the env var between tests reseats the engine because the cache key changes.

WAL mode + foreign keys are enabled on every new SQLite connection. ``ChromaDB``
remains a separate persistent store under ``data_dir() / "chroma"``; we do not
try to unify them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import Base


_engine_cache: tuple[Path, Engine, sessionmaker[Session]] | None = None


def _data_dir() -> Path:
    # Resolved here rather than imported from storage.py to avoid an import cycle
    # (storage.py imports db.py). Mirrors storage.data_dir() byte-for-byte.
    repo_root = Path(__file__).resolve().parents[2]
    return Path(os.environ.get("TRAJECTA_DATA_DIR", repo_root / "data")).resolve()


def _db_path() -> Path:
    return _data_dir() / "trajecta.db"


def _make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine_cache
    target = _db_path()
    if _engine_cache is not None and _engine_cache[0] == target:
        return _engine_cache[1]
    engine = _make_engine(target)
    Session_ = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(engine)
    _engine_cache = (target, engine, Session_)
    return engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _engine_cache is not None
    return _engine_cache[2]


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager yielding a Session that auto-commits on clean exit."""

    Session_ = get_sessionmaker()
    session = Session_()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_schema() -> None:
    """Idempotently create all tables. Safe to call on every startup."""

    get_engine()  # side effect: create_all


def reset_engine_cache() -> None:
    """Drop the cached engine. Used by tests after pointing TRAJECTA_DATA_DIR elsewhere."""

    global _engine_cache
    if _engine_cache is not None:
        _engine_cache[1].dispose()
    _engine_cache = None
