"""M4 credit wallets, append-only ledger, reservations, and packages.

Revision ID: 0004_m4_credit_engine
Revises: 0003_m3_workspaces_rbac
Create Date: 2026-11-18 09:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_m4_credit_engine"
down_revision: str | Sequence[str] | None = "0003_m3_workspaces_rbac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create credit_wallets, credit_transactions, credit_reservations, and credit_packages."""
    op.create_table(
        "credit_wallets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("available_balance", sa.Integer(), nullable=False),
        sa.Column("reserved_balance", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "available_balance >= 0",
            name="ck_credit_wallets_available_balance_non_negative",
        ),
        sa.CheckConstraint(
            "reserved_balance >= 0",
            name="ck_credit_wallets_reserved_balance_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_credit_wallets_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_wallets")),
        sa.UniqueConstraint("workspace_id", name=op.f("uq_credit_wallets_workspace_id")),
    )
    op.create_index(
        op.f("ix_credit_wallets_workspace_id"), "credit_wallets", ["workspace_id"], unique=True
    )

    op.create_table(
        "credit_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reference_type", sa.String(length=64), nullable=True),
        sa.Column("reference_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "transaction_type IN ('purchase', 'grant', 'commit', 'release', "
            "'refund', 'adjustment', 'expiry')",
            name="ck_credit_transactions_transaction_type",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_credit_transactions_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["credit_wallets.id"],
            name=op.f("fk_credit_transactions_wallet_id_credit_wallets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_credit_transactions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_transactions")),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_credit_transactions_workspace_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_credit_transactions_wallet_id"),
        "credit_transactions",
        ["wallet_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_credit_transactions_workspace_id"),
        "credit_transactions",
        ["workspace_id"],
        unique=False,
    )

    op.create_table(
        "credit_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_credit_reservations_amount_positive"),
        sa.CheckConstraint(
            "status IN ('reserved', 'committed', 'released', 'expired')",
            name="ck_credit_reservations_status",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["credit_wallets.id"],
            name=op.f("fk_credit_reservations_wallet_id_credit_wallets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_credit_reservations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_reservations")),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_credit_reservations_workspace_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_credit_reservations_job_id"), "credit_reservations", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_credit_reservations_wallet_id"),
        "credit_reservations",
        ["wallet_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_credit_reservations_workspace_id"),
        "credit_reservations",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_credit_reservations_status_expires",
        "credit_reservations",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "credit_packages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("price_minor_units", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("workspace_type", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("credits > 0", name="ck_credit_packages_credits_positive"),
        sa.CheckConstraint(
            "price_minor_units >= 0", name="ck_credit_packages_price_non_negative"
        ),
        sa.CheckConstraint(
            "workspace_type IN ('personal', 'organization', 'all')",
            name="ck_credit_packages_workspace_type",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_packages")),
    )


def downgrade() -> None:
    """Drop credit_packages, credit_reservations, credit_transactions, and credit_wallets."""
    op.drop_table("credit_packages")

    op.drop_index("ix_credit_reservations_status_expires", table_name="credit_reservations")
    op.drop_index(op.f("ix_credit_reservations_workspace_id"), table_name="credit_reservations")
    op.drop_index(op.f("ix_credit_reservations_wallet_id"), table_name="credit_reservations")
    op.drop_index(op.f("ix_credit_reservations_job_id"), table_name="credit_reservations")
    op.drop_table("credit_reservations")

    op.drop_index(op.f("ix_credit_transactions_workspace_id"), table_name="credit_transactions")
    op.drop_index(op.f("ix_credit_transactions_wallet_id"), table_name="credit_transactions")
    op.drop_table("credit_transactions")

    op.drop_index(op.f("ix_credit_wallets_workspace_id"), table_name="credit_wallets")
    op.drop_table("credit_wallets")

