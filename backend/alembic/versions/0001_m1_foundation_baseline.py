"""M1 foundation baseline revision.

Revision ID: 0001_m1_foundation_baseline
Revises:
Create Date: 2026-11-17 00:00:00.000000
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_m1_foundation_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish baseline Alembic version tracking before M2 identity tables."""


def downgrade() -> None:
    """Revert baseline Alembic revision."""
