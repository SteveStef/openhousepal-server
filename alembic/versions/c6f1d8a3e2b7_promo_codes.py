"""Admin sign-up codes (reusable; a special PayPal plan, or billed by us)

Re-runnable.

Revision ID: c6f1d8a3e2b7
Revises: b4e8a1c6d2f9
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c6f1d8a3e2b7"
down_revision: Union[str, None] = "b4e8a1c6d2f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id VARCHAR PRIMARY KEY,
            code VARCHAR NOT NULL UNIQUE,
            label VARCHAR,
            kind VARCHAR NOT NULL,
            plan_tier VARCHAR NOT NULL,
            paypal_plan_id VARCHAR,
            trial_days INTEGER NOT NULL DEFAULT 0,
            custom_price DOUBLE PRECISION,
            billing_cycle VARCHAR,
            billing_group_id VARCHAR REFERENCES billing_groups(id) ON DELETE SET NULL,
            max_uses INTEGER,
            uses INTEGER NOT NULL DEFAULT 0,
            expires_at TIMESTAMPTZ,
            is_active BOOLEAN NOT NULL DEFAULT true,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_promo_codes_code ON promo_codes (code)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_promo_codes_paypal_plan_id ON promo_codes (paypal_plan_id)")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS promo_code_id VARCHAR REFERENCES promo_codes(id) ON DELETE SET NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_users_promo_code_id ON users (promo_code_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS promo_code_id")
    op.execute("DROP TABLE IF EXISTS promo_codes")
