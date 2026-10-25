"""Forms: send the visitor to the Showcase their answers just built

agent_forms.redirect_to_showcase - after submitting, a form that builds a Showcase (Setup My Own
Showcase, I'm Buying, How Can We Help) opens that Showcase. On by default; the agent can turn it
off in the form's Settings. One new column with a constant default - nothing is backfilled.

Revision ID: d4f6a8c0e2b1
Revises: c7e9a1b3d5f8
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f6a8c0e2b1"
down_revision: Union[str, None] = "c7e9a1b3d5f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_forms", sa.Column("redirect_to_showcase", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("agent_forms", "redirect_to_showcase")
