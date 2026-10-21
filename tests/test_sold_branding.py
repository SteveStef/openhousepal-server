"""Sold-home branding rule (app/services/sold_branding.py): a sold home carries an agent's branding
only when that agent was on the sale (listing / co-listing / buyer's / co-buyer's agent)."""
import asyncio
from types import SimpleNamespace

from app.services import sold_branding as sb


def _agent(mls_id=None, uid="u-1"):
    return SimpleNamespace(id=uid, mls_id=mls_id)


def _home(status="CLOSED-BRIGHT", list_id=None, co_list_id=None, **raw):
    return {"home_status": status, "list_agent_mls_id": list_id, "co_list_agent_mls_id": co_list_id, "raw_mls_data": raw}


def test_sold_and_agent_was_list_agent_is_branded():
    home = _home(list_id="3001234", ListAgentMlsId="3001234", BuyerAgentMlsId="3009999")
    assert sb.agent_took_part_in_sale(_agent("3001234"), home)
    assert sb.sold_branding_allowed(_agent("3001234"), home)


def test_sold_and_agent_was_co_list_agent_is_branded():
    home = _home(list_id="3000001", co_list_id="3001234", CoListAgentMlsId="3001234")
    assert sb.sold_branding_allowed(_agent("3001234"), home)


def test_sold_and_agent_was_buyer_agent_is_branded():
    home = _home(list_id="3000001", BuyerAgentMlsId="3001234")
    assert sb.sold_branding_allowed(_agent("3001234"), home)


def test_sold_and_agent_was_co_buyer_agent_is_branded():
    home = _home(list_id="3000001", BuyerAgentMlsId="3000002", CoBuyerAgentMlsId=" 3001234 ")
    assert sb.sold_branding_allowed(_agent("3001234"), home)


def test_mls_ids_match_ignoring_case_and_spaces():
    home = _home(BuyerAgentMlsId="ab123")
    assert sb.sold_branding_allowed(_agent(" AB123 "), home)


def test_sold_and_unrelated_agent_is_neutral():
    home = _home(list_id="3000001", ListAgentMlsId="3000001", BuyerAgentMlsId="3000002")
    assert not sb.agent_took_part_in_sale(_agent("3001234"), home)
    assert not sb.sold_branding_allowed(_agent("3001234"), home)


def test_sold_and_agent_without_mls_id_is_neutral():
    home = _home(list_id="3000001", BuyerAgentMlsId="3000002")
    assert not sb.sold_branding_allowed(_agent(None), home)
    assert not sb.sold_branding_allowed(_agent("   "), home)


def test_sold_with_no_agent_at_all_is_neutral():
    assert not sb.sold_branding_allowed(None, _home(list_id="3000001"))


def test_sold_home_with_no_agent_ids_on_record_is_neutral():
    assert not sb.sold_branding_allowed(_agent("3001234"), _home())


def test_own_hand_entered_listing_marked_sold_is_branded():
    # Agents outside Bright own their hand-entered listings via "user:<id>" (plan_ids.listing_owner_key).
    home = _home(status="Sold", list_id="user:u-1")
    assert sb.sold_branding_allowed(_agent(None, uid="u-1"), home)
    assert not sb.sold_branding_allowed(_agent(None, uid="u-2"), home)


def test_active_pending_coming_soon_are_branded_as_today():
    for status in ("ACTIVE-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT", "PENDING-BRIGHT", "COMING SOON-BRIGHT", "ACTIVE"):
        home = _home(status=status, list_id="3000001", ListAgentMlsId="3000001")
        assert sb.sold_branding_allowed(_agent("3001234"), home), status
        assert sb.sold_branding_allowed(_agent(None), home), status


def test_sold_detected_from_raw_status_too():
    assert sb.is_sold({"home_status": None, "raw_mls_data": {"StandardStatus": "Closed"}})
    assert sb.is_sold({"home_status": "CLOSED-BRIGHT"})
    assert sb.is_sold({"home_status": "Sold"})
    assert not sb.is_sold({"home_status": "ACTIVE-BRIGHT", "raw_mls_data": {"StandardStatus": "Active"}})
    assert not sb.is_sold(None)


def test_works_on_orm_like_objects():
    prop = SimpleNamespace(home_status="CLOSED-BRIGHT", list_agent_mls_id="3000001", co_list_agent_mls_id=None,
                           raw_mls_data={"BuyerAgentMlsId": "3001234"})
    assert sb.sold_branding_allowed(_agent("3001234"), prop)
    assert not sb.sold_branding_allowed(_agent("3005555"), prop)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    """Answers the one select branding_allowed_for_listing makes, in its column order:
    home_status, list_agent_mls_id, co_list_agent_mls_id, then the raw keys."""
    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _FakeResult(self.row)


def _row(status, list_id=None, co_list_id=None, **raw):
    keys = sb.SALE_AGENT_MLS_FIELDS + ("MlsStatus", "StandardStatus")
    return (status, list_id, co_list_id, *[raw.get(k) for k in keys])


def test_branding_allowed_for_listing_reads_the_mirror():
    run = asyncio.run
    assert run(sb.branding_allowed_for_listing(_FakeDb(_row("CLOSED-BRIGHT", "3000001", BuyerAgentMlsId="3001234")), _agent("3001234"), "LK1"))
    assert not run(sb.branding_allowed_for_listing(_FakeDb(_row("CLOSED-BRIGHT", "3000001", BuyerAgentMlsId="3000002")), _agent("3001234"), "LK1"))
    assert not run(sb.branding_allowed_for_listing(_FakeDb(_row("CLOSED-BRIGHT", "3000001")), _agent(None), "LK1"))
    assert run(sb.branding_allowed_for_listing(_FakeDb(_row("ACTIVE-BRIGHT", "3000001")), _agent("3001234"), "LK1"))


def test_branding_allowed_for_listing_without_a_record_or_key_is_unchanged():
    run = asyncio.run
    assert run(sb.branding_allowed_for_listing(_FakeDb(None), _agent("3001234"), "LK-missing"))
    db = _FakeDb(None)
    assert run(sb.branding_allowed_for_listing(db, _agent("3001234"), None))
    assert db.calls == 0
