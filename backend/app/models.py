"""SQLAlchemy ORM models for Trajecta persistence.

Kept separate from ``backend.app.schemas`` (Pydantic contracts) on purpose:
the API surface and the storage shape are different concerns. ``storage.py``
owns the translation between the two.

Schema design notes:

- Runs and their steps live in two tables (``runs``, ``steps``); per-step
  Pydantic blobs (observation, action, result, coordinate_validation,
  metadata) are stored as JSON columns rather than fully normalized. Runs
  are immutable after import and no query slices into nested step fields,
  so the simpler shape wins. If we ever need ``WHERE action_type = ?``
  filtering, we can add generated columns without a data migration.
- Screenshots are stored as BLOB on a per-(run_id, filename) row. The
  ``trajecta.db`` single-file deployment story is the whole point of this
  refactor; offloading screenshots to the filesystem would defeat it.
- ``digests`` and ``traces`` are 1:1 with runs (latest wins).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    run_metadata: Mapped[dict[str, Any]] = mapped_column("metadata_json", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    steps: Mapped[list["Step"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="Step.step_index",
        lazy="selectin",
    )
    screenshots: Mapped[list["Screenshot"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    digest: Mapped["Digest | None"] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        uselist=False,
    )
    trace: Mapped["Trace | None"] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Step(Base):
    __tablename__ = "steps"

    run_id: Mapped[str] = mapped_column(
        String(256), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    step_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    coordinate_validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    step_metadata: Mapped[dict[str, Any]] = mapped_column("metadata_json", JSON, nullable=False, default=dict)

    run: Mapped[Run] = relationship(back_populates="steps")


class Screenshot(Base):
    __tablename__ = "screenshots"

    run_id: Mapped[str] = mapped_column(
        String(256), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    filename: Mapped[str] = mapped_column(String(256), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/png")
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    run: Mapped[Run] = relationship(back_populates="screenshots")


class Digest(Base):
    __tablename__ = "digests"

    run_id: Mapped[str] = mapped_column(
        String(256), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    run: Mapped[Run] = relationship(back_populates="digest")


class Trace(Base):
    __tablename__ = "traces"

    run_id: Mapped[str] = mapped_column(
        String(256), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    run: Mapped[Run] = relationship(back_populates="trace")


class EvalCaseRow(Base):
    __tablename__ = "eval_cases"

    case_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    human_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class FailureMemoryRow(Base):
    __tablename__ = "failure_memory"

    case_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
