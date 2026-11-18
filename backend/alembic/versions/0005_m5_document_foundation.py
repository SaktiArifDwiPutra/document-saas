"""M5 document foundation: templates, documents, versions, sources, jobs, callbacks.

Revision ID: 0005_m5_document_foundation
Revises: 0004_m4_credit_engine
Create Date: 2026-11-18 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.base import NAMING_CONVENTION

# revision identifiers, used by Alembic.
revision: str = "0005_m5_document_foundation"
down_revision: str | Sequence[str] | None = "0004_m4_credit_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONBVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Create M5 tables and link credit_reservations.job_id to jobs.id."""
    # 1. Templates foundation (created before documents for selected_template_version_id FK).
    op.create_table(
        "templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_templates_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_templates_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_templates_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_templates")),
    )
    op.create_index(op.f("ix_templates_document_type"), "templates", ["document_type"])
    op.create_index(op.f("ix_templates_workspace_id"), "templates", ["workspace_id"])

    op.create_table(
        "template_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("storage_object_key", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("placeholder_schema", JSONBVariant, nullable=False),
        sa.Column("mapping", JSONBVariant, nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "version_number >= 1",
            name="ck_template_versions_version_number_positive",
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'invalid')",
            name="ck_template_versions_validation_status",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["templates.id"],
            name=op.f("fk_template_versions_template_id_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_versions")),
        sa.UniqueConstraint(
            "template_id",
            "version_number",
            name="uq_template_versions_number",
        ),
    )
    op.create_index(
        op.f("ix_template_versions_template_id"),
        "template_versions",
        ["template_id"],
    )

    # 2. Documents (circular FKs to document_versions added after document_versions exists).
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("selected_template_version_id", sa.Uuid(), nullable=True),
        sa.Column("approved_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'processing', 'review_required', 'approved', "
            "'rendering', 'completed', 'failed', 'archived')",
            name="ck_documents_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_documents_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["selected_template_version_id"],
            ["template_versions.id"],
            name=op.f("fk_documents_selected_template_version_id_template_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_documents_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_created_by_user_id"), "documents", ["created_by_user_id"])
    op.create_index(op.f("ix_documents_document_type"), "documents", ["document_type"])
    op.create_index(op.f("ix_documents_workspace_id"), "documents", ["workspace_id"])
    op.create_index(
        "ix_documents_workspace_created",
        "documents",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_documents_workspace_status",
        "documents",
        ["workspace_id", "status"],
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("structured_content", JSONBVariant, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_status", sa.String(length=32), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "version_number >= 1",
            name="ck_document_versions_version_number_positive",
        ),
        sa.CheckConstraint(
            "approval_status IN ('pending', 'approved', 'superseded', 'rejected')",
            name="ck_document_versions_approval_status",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_versions_approved_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_versions_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_document_versions_document_version",
        ),
    )
    op.create_index(
        op.f("ix_document_versions_document_id"),
        "document_versions",
        ["document_id"],
    )

    with op.batch_alter_table("documents", naming_convention=NAMING_CONVENTION) as batch_op:
        batch_op.create_foreign_key(
            "fk_documents_current_version_id",
            "document_versions",
            ["current_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_documents_approved_version_id",
            "document_versions",
            ["approved_version_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 3. Document sources with workspace and source_bundle_id binding.
    op.create_table(
        "document_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=True),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("storage_object_key", sa.String(length=512), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("extracted_text_reference", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata", JSONBVariant, nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('form', 'text', 'upload', 'workspace_reference')",
            name="ck_document_sources_source_type",
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'valid', 'rejected')",
            name="ck_document_sources_validation_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_sources_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_sources_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_sources_document_version_id_document_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_document_sources_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_sources")),
    )
    op.create_index(
        op.f("ix_document_sources_document_id"),
        "document_sources",
        ["document_id"],
    )
    op.create_index(
        op.f("ix_document_sources_document_version_id"),
        "document_sources",
        ["document_version_id"],
    )
    op.create_index(
        op.f("ix_document_sources_source_bundle_id"),
        "document_sources",
        ["source_bundle_id"],
    )
    op.create_index(
        op.f("ix_document_sources_workspace_id"),
        "document_sources",
        ["workspace_id"],
    )
    op.create_index(
        "ix_document_sources_document_created",
        "document_sources",
        ["document_id", "created_at"],
    )

    # 4. Jobs, job attempts, and workflow callback audit records.
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_bundle_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_key", sa.String(length=128), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("workflow_provider", sa.String(length=64), nullable=False),
        sa.Column("external_execution_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_error_message", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "job_type IN ('normalize', 'ai_generation', 'docx_render')",
            name="ck_jobs_job_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'validating', 'completed', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_jobs_attempt_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_jobs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_jobs_workspace_idempotency",
        ),
    )
    op.create_index(op.f("ix_jobs_document_id"), "jobs", ["document_id"])
    op.create_index(op.f("ix_jobs_external_execution_id"), "jobs", ["external_execution_id"])
    op.create_index(op.f("ix_jobs_source_bundle_id"), "jobs", ["source_bundle_id"])
    op.create_index(op.f("ix_jobs_workspace_id"), "jobs", ["workspace_id"])
    op.create_index("ix_jobs_document_created", "jobs", ["document_id", "created_at"])
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"])
    op.create_index("ix_jobs_workspace_created", "jobs", ["workspace_id", "created_at"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_execution_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_metadata", JSONBVariant, nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_job_attempts_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_job_attempts_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_job_attempts_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_attempts")),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
    )
    op.create_index(op.f("ix_job_attempts_job_id"), "job_attempts", ["job_id"])

    op.create_table(
        "workflow_callback_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("source_service", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "source_service IN ('workflow', 'worker')",
            name="ck_workflow_callback_events_source_service",
        ),
        sa.CheckConstraint(
            "event_type IN ('completed', 'failed')",
            name="ck_workflow_callback_events_event_type",
        ),
        sa.CheckConstraint(
            "result_status IN ('processed', 'ignored_duplicate', 'rejected')",
            name="ck_workflow_callback_events_result_status",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_workflow_callback_events_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_callback_events")),
        sa.UniqueConstraint(
            "event_id",
            name=op.f("uq_workflow_callback_events_event_id"),
        ),
    )
    op.create_index(
        op.f("ix_workflow_callback_events_job_id"),
        "workflow_callback_events",
        ["job_id"],
    )

    # 5. Link M4 credit_reservations.job_id to M5 jobs.id now that jobs exists.
    with op.batch_alter_table(
        "credit_reservations", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_credit_reservations_job_id",
            "jobs",
            ["job_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Drop M5 foreign key links and tables in reverse dependency order."""
    with op.batch_alter_table(
        "credit_reservations", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint("fk_credit_reservations_job_id", type_="foreignkey")

    op.drop_index(
        op.f("ix_workflow_callback_events_job_id"),
        table_name="workflow_callback_events",
    )
    op.drop_table("workflow_callback_events")

    op.drop_index(op.f("ix_job_attempts_job_id"), table_name="job_attempts")
    op.drop_table("job_attempts")

    op.drop_index("ix_jobs_workspace_created", table_name="jobs")
    op.drop_index("ix_jobs_status_created", table_name="jobs")
    op.drop_index("ix_jobs_document_created", table_name="jobs")
    op.drop_index(op.f("ix_jobs_workspace_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_source_bundle_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_external_execution_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_document_id"), table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_document_sources_document_created", table_name="document_sources")
    op.drop_index(op.f("ix_document_sources_workspace_id"), table_name="document_sources")
    op.drop_index(op.f("ix_document_sources_source_bundle_id"), table_name="document_sources")
    op.drop_index(
        op.f("ix_document_sources_document_version_id"),
        table_name="document_sources",
    )
    op.drop_index(op.f("ix_document_sources_document_id"), table_name="document_sources")
    op.drop_table("document_sources")

    with op.batch_alter_table("documents", naming_convention=NAMING_CONVENTION) as batch_op:
        batch_op.drop_constraint("fk_documents_approved_version_id", type_="foreignkey")
        batch_op.drop_constraint("fk_documents_current_version_id", type_="foreignkey")

    op.drop_index(op.f("ix_document_versions_document_id"), table_name="document_versions")
    op.drop_table("document_versions")

    op.drop_index("ix_documents_workspace_status", table_name="documents")
    op.drop_index("ix_documents_workspace_created", table_name="documents")
    op.drop_index(op.f("ix_documents_workspace_id"), table_name="documents")
    op.drop_index(op.f("ix_documents_document_type"), table_name="documents")
    op.drop_index(op.f("ix_documents_created_by_user_id"), table_name="documents")
    op.drop_table("documents")

    op.drop_index(op.f("ix_template_versions_template_id"), table_name="template_versions")
    op.drop_table("template_versions")

    op.drop_index(op.f("ix_templates_workspace_id"), table_name="templates")
    op.drop_index(op.f("ix_templates_document_type"), table_name="templates")
    op.drop_table("templates")

