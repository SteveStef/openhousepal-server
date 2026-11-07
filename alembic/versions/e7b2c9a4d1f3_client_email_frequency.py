"""How often a client gets automatic emails, when the agent or the client picked it

Revision ID: e7b2c9a4d1f3
Revises: c1f4a9d2e7b6
"""
from alembic import op
import sqlalchemy as sa

revision = "e7b2c9a4d1f3"
down_revision = "c1f4a9d2e7b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("client_email_prefs", sa.Column("frequency", sa.String(), nullable=True))
    op.add_column("client_email_prefs", sa.Column("frequency_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("client_email_prefs", "frequency_by")
    op.drop_column("client_email_prefs", "frequency")
