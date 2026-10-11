"""drop the stale ai_assistant_config snapshot row

AIConfigService.get_config used to save a full copy of DEFAULT_CONFIG into system_settings the
first time AI ran. That saved copy outranks the code, so every default changed since (the CMA
platform caps of $50/day and $150/month, etc.) never reached an environment that already had it.
The service no longer writes that copy; this removes the old one so the code defaults apply.

Nothing in the app writes deliberate overrides to this row (there is no admin editor for it), so
it can only be the snapshot. It is kept under a backup key anyway, never read by the app.

Revision ID: c3e8f1a2b4d6
Revises: b2d7f4a9c1e3
Create Date: 2026-09-28
"""
from alembic import op

revision = "c3e8f1a2b4d6"
down_revision = "b2d7f4a9c1e3"
branch_labels = None
depends_on = None

BACKUP_KEY = "ai_assistant_config_backup_2026_09_28"


def upgrade() -> None:
    op.execute(f"""
        INSERT INTO system_settings (key, value)
        SELECT '{BACKUP_KEY}', value FROM system_settings WHERE key = 'ai_assistant_config'
        ON CONFLICT (key) DO NOTHING
    """)
    op.execute("DELETE FROM system_settings WHERE key = 'ai_assistant_config'")


def downgrade() -> None:
    op.execute(f"""
        INSERT INTO system_settings (key, value)
        SELECT 'ai_assistant_config', value FROM system_settings WHERE key = '{BACKUP_KEY}'
        ON CONFLICT (key) DO NOTHING
    """)
