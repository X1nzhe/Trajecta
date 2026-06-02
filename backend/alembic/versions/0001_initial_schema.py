"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-24

Mirrors ``backend.app.models``. The app also calls ``Base.metadata.create_all``
on startup for dev simplicity (idempotent), so this revision exists primarily
as the canonical migration target for production environments and as the
baseline that future revisions chain off of.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trajectories",
        sa.Column("trajectory_id", sa.String(length=256), primary_key=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Index name matches ``ix_%(table_name)s_%(column_0_name)s`` from
    # ``backend.app.models.NAMING_CONVENTION`` so create_all and Alembic stay
    # in sync; do not rename without updating both sides.
    op.create_index("ix_trajectories_status", "trajectories", ["status"])

    op.create_table(
        "steps",
        sa.Column("trajectory_id", sa.String(length=256), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.String(length=64), nullable=True),
        sa.Column("observation_json", sa.JSON(), nullable=False),
        sa.Column("action_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("coordinate_validation_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("trajectory_id", "step_index"),
        sa.ForeignKeyConstraint(["trajectory_id"], ["trajectories.trajectory_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "screenshots",
        sa.Column("trajectory_id", sa.String(length=256), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("trajectory_id", "filename"),
        sa.ForeignKeyConstraint(["trajectory_id"], ["trajectories.trajectory_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "digests",
        sa.Column("trajectory_id", sa.String(length=256), primary_key=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trajectory_id"], ["trajectories.trajectory_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "traces",
        sa.Column("trajectory_id", sa.String(length=256), primary_key=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trajectory_id"], ["trajectories.trajectory_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "eval_cases",
        sa.Column("case_id", sa.String(length=256), primary_key=True),
        sa.Column("source_trajectory_id", sa.String(length=256), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("human_validated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_eval_cases_source_trajectory_id", "eval_cases", ["source_trajectory_id"])

    op.create_table(
        "failure_memory",
        sa.Column("case_id", sa.String(length=256), primary_key=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("failure_memory")
    op.drop_index("ix_eval_cases_source_trajectory_id", table_name="eval_cases")
    op.drop_table("eval_cases")
    op.drop_table("traces")
    op.drop_table("digests")
    op.drop_table("screenshots")
    op.drop_table("steps")
    op.drop_index("ix_trajectories_status", table_name="trajectories")
    op.drop_table("trajectories")
