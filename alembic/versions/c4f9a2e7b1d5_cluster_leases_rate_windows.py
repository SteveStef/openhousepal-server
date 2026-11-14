"""Shared coordination for several API copies (WEB_WORKERS > 1): cluster_leases ("this is
running / taken", expires if a copy dies) and rate_windows (speed limits counted across copies).
See app/services/cluster.py. Two new small tables - instant, nothing existing is touched.

Revision ID: c4f9a2e7b1d5
Revises: b3e8d1f6a2c4
Create Date: 2026-11-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4f9a2e7b1d5"
down_revision: Union[str, None] = "b3e8d1f6a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS cluster_leases (
            kind VARCHAR(40) NOT NULL,
            key VARCHAR(200) NOT NULL,
            token VARCHAR(40) NOT NULL,
            owner_id VARCHAR NULL,
            amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (kind, key)
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cluster_leases_owner ON cluster_leases (kind, owner_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cluster_leases_expires ON cluster_leases (expires_at)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS rate_windows (
            key VARCHAR(200) NOT NULL,
            window_start BIGINT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, window_start)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rate_windows")
    op.execute("DROP TABLE IF EXISTS cluster_leases")
