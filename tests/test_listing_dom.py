from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.property import PropertyDetailResponse, PropertySummaryResponse
from app.services.cma_ai_service import _reconciled_subject_snapshot, _subject_status_timeline
from app.services.collections_service import CollectionsService
from app.utils.listing_dom import is_actively_marketed, resolve_days_on_market


AS_OF = datetime(2026, 9, 11, tzinfo=timezone.utc)
LISTED = datetime(2026, 7, 15, tzinfo=timezone.utc)


def _property_payload(*, status: str, dom):
    return {
        "id": "property-1",
        "ListingKey": "test-listing-1",
        "FullStreetAddress": "100 TEST RD",
        "City": "ARDMORE",
        "StateOrProvince": "PA",
        "ListPrice": 3_199_000,
        "MlsStatus": status,
        "DaysOnMarket": dom,
        "MLSListDate": LISTED,
    }


@pytest.mark.parametrize(
    "status",
    [
        "ACTIVE-BRIGHT",
        "COMING SOON-BRIGHT",
        "CONTINGENT-BRIGHT",
        "ACTIVE UNDER CONTRACT-BRIGHT",
        "PENDING-BRIGHT",
        "CLOSED-BRIGHT",
        "WITHDRAWN-BRIGHT",
        "EXPIRED-BRIGHT",
        "CANCELED-BRIGHT",
    ],
)
def test_explicit_bright_dom_is_never_replaced(status):
    assert resolve_days_on_market(17, status, LISTED, as_of=AS_OF) == 17


@pytest.mark.parametrize(
    "status",
    [
        "CONTINGENT-BRIGHT",
        "ACTIVE UNDER CONTRACT-BRIGHT",
        "PENDING-BRIGHT",
        "CLOSED-BRIGHT",
        "WITHDRAWN-BRIGHT",
        "EXPIRED-BRIGHT",
        "CANCELED-BRIGHT",
    ],
)
def test_non_active_listing_without_bright_dom_never_counts_to_today(status):
    assert resolve_days_on_market(None, status, LISTED, as_of=AS_OF) is None


@pytest.mark.parametrize("status", ["ACTIVE-BRIGHT", "ACTIVE", "COMING SOON-BRIGHT", "Coming Soon"])
def test_active_listing_uses_date_fallback_only_when_bright_dom_is_missing(status):
    assert is_actively_marketed(status)
    assert resolve_days_on_market(None, status, LISTED, as_of=AS_OF) == 58


def test_property_response_models_preserve_bright_dom_for_contingent_listing():
    summary = PropertySummaryResponse.model_validate(
        _property_payload(status="CONTINGENT-BRIGHT", dom=17)
    )
    detail = PropertyDetailResponse.model_validate(
        _property_payload(status="CONTINGENT-BRIGHT", dom=17)
    )

    assert summary.days_on_market == 17
    assert detail.days_on_market == 17


def test_property_response_does_not_invent_dom_for_pending_listing_when_missing():
    detail = PropertyDetailResponse.model_validate(
        _property_payload(status="PENDING-BRIGHT", dom=None)
    )
    assert detail.days_on_market is None


def test_collections_use_the_same_source_preserving_policy():
    assert CollectionsService._calculate_dom(LISTED, 17, "CONTINGENT-BRIGHT") == 17
    assert CollectionsService._calculate_dom(LISTED, None, "CONTINGENT-BRIGHT") is None


def test_cma_explicit_contract_date_reconciles_stale_bright_dom_and_subject_snapshot():
    subject = {
        "source": "mls",
        "listing_key": "test-listing-1",
        "mls_status": "ACTIVE-BRIGHT",
        "days_on_market": 58,
        "list_price": 3_199_000,
    }
    stored_property = SimpleNamespace(
        home_status="ACTIVE-BRIGHT",
        days_on_market=2,
        cumulative_days_on_market=None,
        mls_list_date=LISTED,
        original_list_price=3_199_000,
        price=3_199_000,
        raw_mls_data={
            "MlsStatus": "ACTIVE-BRIGHT",
            "DaysOnMarket": 2,
            "MLSListDate": "2026-07-15",
            "ListPrice": 3_199_000,
        },
    )
    live_overlay = {
        "MlsStatus": "ACTIVE UNDER CONTRACT-BRIGHT",
        "StandardStatus": "Active Under Contract",
        "DaysOnMarket": 17,
        "PurchaseContractDate": "2026-07-29",
    }

    timeline = _subject_status_timeline(subject, stored_property, live_overlay=live_overlay)
    reconciled = _reconciled_subject_snapshot(subject, timeline)

    assert timeline["current_status"] == "Active Under Contract"
    assert timeline["active_market_days"] == 14
    assert timeline["dom_source"] == "list date -> under-contract date"
    assert reconciled["days_on_market"] == 14
    assert reconciled["mls_status"] == "Active Under Contract"


def test_cma_documented_interruption_preserves_bright_dom_total():
    stored_property = SimpleNamespace(
        home_status="ACTIVE UNDER CONTRACT-BRIGHT",
        days_on_market=17,
        cumulative_days_on_market=None,
        mls_list_date=LISTED,
        original_list_price=3_199_000,
        price=3_199_000,
        raw_mls_data={
            "MlsStatus": "ACTIVE UNDER CONTRACT-BRIGHT",
            "DaysOnMarket": 17,
            "MLSListDate": "2026-07-15",
            "PurchaseContractDate": "2026-07-29",
            "MajorChangeType": "Back On Market",
        },
    )
    timeline = _subject_status_timeline({}, stored_property)

    assert timeline["active_market_days"] == 17
    assert timeline["dom_source"] == "MLS DaysOnMarket field"


def test_manual_cma_subject_is_not_overwritten_by_mls_reconciliation():
    subject = {"source": "manual", "days_on_market": 12, "mls_status": "Active"}
    timeline = {"active_market_days": 17, "current_status": "Active Under Contract"}
    assert _reconciled_subject_snapshot(subject, timeline) is subject
