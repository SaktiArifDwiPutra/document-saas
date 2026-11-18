"""M6 AI engine: usage_events telemetry table.

Revision ID: 0006_m6_ai_engine
Revises: 0005_m5_document_foundation
Create Date: 2026-11-18 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_m6_ai_engine"
down_revision: str | Sequence[str] | None = "0005_m5_document_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create usage_events table for token/latency telemetry without storing source text."""
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("workflow_key", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_minor_units", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "input_tokens >= 0",
            name="ck_usage_events_input_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "output_tokens >= 0",
            name="ck_usage_events_output_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "estimated_cost_minor_units >= 0",
            name="ck_usage_events_estimated_cost_non_negative",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_usage_events_duration_ms_non_negative",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_usage_events_status",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_usage_events_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_usage_events_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_usage_events_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_usage_events_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
    )
    op.create_index(op.f("ix_usage_events_document_id"), "usage_events", ["document_id"])
    op.create_index(op.f("ix_usage_events_job_id"), "usage_events", ["job_id"])
    op.create_index(op.f("ix_usage_events_user_id"), "usage_events", ["user_id"])
    op.create_index(op.f("ix_usage_events_workspace_id"), "usage_events", ["workspace_id"])
    op.create_index(
        "ix_usage_events_workspace_created",
        "usage_events",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    """Drop usage_events indexes and table."""
    op.drop_index("ix_usage_events_workspace_created", table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_workspace_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_user_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_job_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_document_id"), table_name="usage_events")
    op.drop_table("usage_events")

