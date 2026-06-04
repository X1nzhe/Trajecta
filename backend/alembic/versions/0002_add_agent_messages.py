"""add agent_messages replay buffer

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-02

Adds the ``agent_messages`` table — the opaque LLM conversation replay buffer
(see ``backend.app.models.AgentMessages``). 1:1 with a trajectory, mirrors the
``traces`` table shape. ``Base.metadata.create_all`` also creates this on app
startup for dev (idempotent); this revision is the canonical migration target
for production and the baseline future revisions chain off of.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("trajectory_id", sa.String(length=256), primary_key=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trajectory_id"], ["trajectories.trajectory_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("agent_messages")
