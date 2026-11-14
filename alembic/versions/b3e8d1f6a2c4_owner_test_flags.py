"""Owner test flags: an agent's own test sign-in / form fill is kept but left out of stats
(owner, 11/13 - "no stats should count the signed-in agent")

Revision ID: b3e8d1f6a2c4
Revises: a9c4e2f7d1b3
Create Date: 2026-11-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3e8d1f6a2c4"
down_revision: Union[str, None] = "a9c4e2f7d1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no default - instant (catalog-only) on both tables.
    op.add_column("open_house_visitors", sa.Column("owner_test", sa.Boolean(), nullable=True))
    op.add_column("form_submissions", sa.Column("owner_test", sa.Boolean(), nullable=True))
    # Past self-tests: sign-ins / fills made with the agent's own account email. Both tables are
    # small (thousands of rows, not the 12 GB properties table) and only matching rows are touched.
    op.execute("""
        UPDATE open_house_visitors v SET owner_test = TRUE
        FROM open_house_events e JOIN users u ON u.id = e.agent_id
        WHERE v.open_house_event_id = e.id AND lower(trim(v.email)) = lower(u.email)
    """)
    op.execute("""
        UPDATE form_submissions s SET owner_test = TRUE
        FROM users u
        WHERE u.id = s.owner_id AND s.contact_email IS NOT NULL AND lower(trim(s.contact_email)) = lower(u.email)
    """)


def downgrade() -> None:
    op.drop_column("form_submissions", "owner_test")
    op.drop_column("open_house_visitors", "owner_test")
