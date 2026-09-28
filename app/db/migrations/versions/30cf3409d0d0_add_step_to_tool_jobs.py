"""add step to tool_jobs

Revision ID: 30cf3409d0d0
Revises: ca0ca6ee4c59
Create Date: 2026-09-27 11:24:57.996642

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "30cf3409d0d0"
down_revision: str | Sequence[str] | None = "ca0ca6ee4c59"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_jobs", sa.Column("step", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tool_jobs", "step")
