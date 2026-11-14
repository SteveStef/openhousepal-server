"""MLS mirror columns: sold date/price and other searched Bright fields as real columns (owner, 11/13)

Every sold-home search, count and sort read raw_mls_data (the 10 GB Bright payload store) just to
find CloseDate / ClosePrice - a 5-town, 1-year lookup took 5.6 s on live. These columns mirror the
payload exactly and are filled BY THE DATABASE: trigger properties_mls_mirror_trg recomputes them
from raw_mls_data on every INSERT and every UPDATE that writes raw_mls_data, whatever code path does
it, so they can't drift. This migration only adds the (empty) columns, the functions and the
trigger - all quick. Existing rows are filled in small batches by app/services/mls_mirror.py (which
also builds the indexes CONCURRENTLY afterwards), never here: a table rewrite or a plain CREATE
INDEX on this table would block the live MLS sync for many minutes.

Keep FIELDS identical to app/services/mls_mirror.py FIELDS (tests/test_mls_mirror.py checks).

Revision ID: 05333178db78
Revises: f7b2d4a6c8e1
Create Date: 2026-11-13
"""
import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "05333178db78"
down_revision: Union[str, None] = "f7b2d4a6c8e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# column -> (Bright key, kind): text = raw->>key, num = its numeric value, json = raw->key
FIELDS = [
    ("mls_close_date", "CloseDate", "text"),
    ("mls_close_price", "ClosePrice", "num"),
    ("mls_purchase_contract_date", "PurchaseContractDate", "text"),
    ("mls_expected_on_market_date", "ExpectedOnMarketDate", "text"),
    ("mls_internet_display", "InternetEntireListingDisplayYN", "text"),
    ("mls_internet_address_display", "InternetAddressDisplayYN", "text"),
    ("mls_association_fee2", "AssociationFee2", "num"),
    ("mls_association_fee2_frequency", "AssociationFee2Frequency", "text"),
    ("mls_buyer_financing", "BuyerFinancing", "json"),
    ("mls_property_condition", "PropertyCondition", "json"),
]
_FN = {"text": "ohp_mls_text", "num": "ohp_mls_num", "json": "ohp_mls_json"}
_SQLTYPE = {"text": "text", "num": "numeric", "json": "jsonb"}

FUNCTIONS = [
    # exactly raw_mls_data->>'Key'
    r"""CREATE OR REPLACE FUNCTION ohp_mls_text(r jsonb, k text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN jsonb_typeof(r) = 'object' THEN r ->> k END
$$""",
    # exactly raw_mls_data->'Key' (a JSON null is SQL NULL, as Python reads it)
    r"""CREATE OR REPLACE FUNCTION ohp_mls_json(r jsonb, k text) RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN jsonb_typeof(r) = 'object' AND jsonb_typeof(r -> k) IS DISTINCT FROM 'null' THEN r -> k END
$$""",
    # a JSON number, or a string holding a plain number; anything else is NULL, never an error
    r"""CREATE OR REPLACE FUNCTION ohp_mls_num(r jsonb, k text) RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN jsonb_typeof(r) IS DISTINCT FROM 'object' THEN NULL
    WHEN jsonb_typeof(r -> k) = 'number' THEN (r ->> k)::numeric
    WHEN jsonb_typeof(r -> k) = 'string' AND btrim(r ->> k) ~ '^-?[0-9]+(\.[0-9]+)?$' THEN btrim(r ->> k)::numeric
  END
$$""",
]


def trigger_function_sql() -> str:
    sets = "\n".join(f"  NEW.{col} := {_FN[kind]}(NEW.raw_mls_data, '{key}');" for col, key, kind in FIELDS)
    return ("CREATE OR REPLACE FUNCTION properties_mls_mirror() RETURNS trigger LANGUAGE plpgsql AS $$\nBEGIN\n"
            f"{sets}\n  NEW.mls_mirrored_at := now();\n  RETURN NEW;\nEND\n$$")


TRIGGER_SQL = ("CREATE OR REPLACE TRIGGER properties_mls_mirror_trg BEFORE INSERT OR UPDATE OF raw_mls_data ON properties "
               "FOR EACH ROW EXECUTE FUNCTION properties_mls_mirror()")


def _with_short_lock(conn, sql: str) -> None:
    """ALTER TABLE / CREATE TRIGGER need a brief exclusive lock on properties. Never sit in the
    lock queue behind a long reader (the nightly pg_dump) and stall every query that arrives after
    us: wait at most 10 s per try, and keep trying for a few minutes."""
    last = None
    for _ in range(30):
        try:
            with conn.begin_nested():
                conn.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
                conn.exec_driver_sql(sql)
            conn.exec_driver_sql("SET LOCAL lock_timeout TO DEFAULT")
            return
        except sa.exc.DBAPIError as e:
            if "lock" not in str(e).lower() or "timeout" not in str(e).lower():
                raise
            last = e
            time.sleep(2)
    raise last


def upgrade() -> None:
    conn = op.get_bind()
    adds = ", ".join(f"ADD COLUMN IF NOT EXISTS {col} {_SQLTYPE[kind]}" for col, _, kind in FIELDS)
    _with_short_lock(conn, f"ALTER TABLE properties {adds}, ADD COLUMN IF NOT EXISTS mls_mirrored_at timestamptz")
    for sql in FUNCTIONS:
        conn.exec_driver_sql(sql)
    conn.exec_driver_sql(trigger_function_sql())
    _with_short_lock(conn, TRIGGER_SQL)


def downgrade() -> None:
    conn = op.get_bind()
    _with_short_lock(conn, "DROP TRIGGER IF EXISTS properties_mls_mirror_trg ON properties")
    conn.exec_driver_sql("DROP FUNCTION IF EXISTS properties_mls_mirror()")
    for idx in ("ix_properties_mls_unmirrored", "ix_properties_mls_close_date", "ix_properties_school_district_trgm"):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx}")
    drops = ", ".join(f"DROP COLUMN IF EXISTS {col}" for col, _, _ in FIELDS)
    _with_short_lock(conn, f"ALTER TABLE properties {drops}, DROP COLUMN IF EXISTS mls_mirrored_at")
    for fn in ("ohp_mls_text", "ohp_mls_json", "ohp_mls_num"):
        conn.exec_driver_sql(f"DROP FUNCTION IF EXISTS {fn}(jsonb, text)")
