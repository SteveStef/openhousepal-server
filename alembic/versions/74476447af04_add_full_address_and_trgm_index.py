"""add_full_address_and_trgm_index

Revision ID: 74476447af04
Revises: c8835a9a07fc
Create Date: 2026-04-29 23:19:15.954050

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '74476447af04'
down_revision: Union[str, None] = 'c8835a9a07fc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Enable pg_trgm extension for fuzzy/partial string matching
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    
    # 2. Add the full_address column
    op.add_column('properties', sa.Column('full_address', sa.String(), nullable=True))
    
    # 3. Backfill existing data
    # Formats as: "123 Main St, City, State Zip"
    op.execute("""
        UPDATE properties 
        SET full_address = street_address || ', ' || city || ', ' || state || ' ' || COALESCE(zipcode, '')
    """)
    
    # 4. Create the Trigram index for ultra-fast searching
    # We use a GIN index which is optimized for these types of multi-character searches
    op.execute("CREATE INDEX idx_properties_full_address_trgm ON properties USING gin (full_address gin_trgm_ops)")


def downgrade() -> None:
    # Remove the index and the column
    op.execute("DROP INDEX IF EXISTS idx_properties_full_address_trgm")
    op.drop_column('properties', 'full_address')
