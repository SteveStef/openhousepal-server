"""Clear the Agent Hub editor's placeholder headshot

The hub editor used to start the agent photo as '/home.jpg' (a stock house picture) and saved it
as profilePhotoUrl when the agent hadn't uploaded one. Every reader (hub, signature, PDFs,
Guest Pass, CMA) then preferred that over the real Settings photo. Drop it so they fall back to
the agent's own photo or initials. Re-runnable.

Revision ID: a3d9e2f7c5b1
Revises: f1c8a3e6b2d4
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a3d9e2f7c5b1"
down_revision: Union[str, None] = "f1c8a3e6b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE agent_hubs SET config = config - 'profilePhotoUrl' WHERE config->>'profilePhotoUrl' = '/home.jpg'")


def downgrade() -> None:
    pass
