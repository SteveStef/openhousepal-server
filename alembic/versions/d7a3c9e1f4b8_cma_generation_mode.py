"""CMA reports remember how their AI run was asked for: "save_usage" (the slower, half-price line) or "now".
A Save usage run can take up to about 25 minutes, so the "stuck run" timer and the page need to know which kind is
going (cma_service._reconcile_stale_generation). One nullable column - instant, nothing existing is touched.

Revision ID: d7a3c9e1f4b8
Revises: c4f9a2e7b1d5
Create Date: 2026-11-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d7a3c9e1f4b8"
down_revision: Union[str, None] = "c4f9a2e7b1d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE cma_reports ADD COLUMN IF NOT EXISTS generation_mode VARCHAR(20) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE cma_reports DROP COLUMN IF EXISTS generation_mode")
