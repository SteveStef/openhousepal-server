"""CMA test 11/13 - the final fact gate (_auto_repair_export_blocks) edits comp notes in place AFTER the
report's comparables were assigned and committed, so its fixes were never saved: the QA flags said
"dropped a 8.8-acre lot size" (418 Reedville Rd) while the stored note still said it.
cma_ai_service._persist_comp_note_fixes re-assigns them. The DB test runs on the local Postgres in one
rolled-back transaction (skipped when it isn't reachable)."""
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.cma_ai_service import _persist_comp_note_fixes  # noqa: E402


def test_same_list_is_reassigned_as_a_new_object():
    comps = [{"listing_key": "a", "commentary": "fixed"}]
    report = SimpleNamespace(comparables=comps)
    _persist_comp_note_fixes(report, comps)
    assert report.comparables is not comps and report.comparables == comps


def test_a_reordered_copy_gets_the_fixed_notes_by_key():
    comps = [{"listing_key": "a", "commentary": "A fixed", "ai_commentary": "A fixed"},
             {"listing_key": "b", "commentary": "B fixed", "ai_commentary": "B fixed"}]
    report = SimpleNamespace(comparables=[{"listing_key": "b", "commentary": "B old", "order_index": 0},
                                          {"listing_key": "a", "commentary": "A old", "order_index": 1}])
    _persist_comp_note_fixes(report, comps)
    assert [(c["listing_key"], c["commentary"], c["order_index"]) for c in report.comparables] == [("b", "B fixed", 0), ("a", "A fixed", 1)]


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


@pytest.mark.asyncio
async def test_in_place_note_fix_after_a_commit_is_saved():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.models.database import CmaReport, User
    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        tag = uuid.uuid4().hex[:8]
        user = User(id=str(uuid.uuid4()), email=f"notes-{tag}@example.com", hashed_password="x", first_name="N",
                    last_name="Otes", state="PA", brokerage="Test Realty", plan_tier="IDX", subscription_status="ACTIVE")
        db.add(user)
        await db.flush()
        report = CmaReport(id=str(uuid.uuid4()), owner_id=user.id, mode="listing", status="DRAFT", subject={}, comparables=[])
        db.add(report)
        await db.commit()
        new_comparables = [{"listing_key": "x", "commentary": "on a roughly 8.8-acre lot"}]
        report.comparables = new_comparables          # assigned and committed (as the engine does) ...
        await db.commit()
        new_comparables[0]["commentary"] = "on a lot"  # ... then the fact gate edits it in place
        _persist_comp_note_fixes(report, new_comparables)
        await db.commit()
        saved = (await db.execute(text("select comparables::text from cma_reports where id=:i"), {"i": report.id})).scalar_one()
        assert "8.8-acre" not in saved and "on a lot" in saved
    finally:
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()
