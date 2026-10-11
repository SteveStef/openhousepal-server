from datetime import date, datetime, timezone
from typing import Optional, Union


DateLike = Union[date, datetime]


def _normalized_status(status: Optional[str]) -> str:
    return "".join(ch for ch in str(status or "").upper() if ch.isalnum())


def is_actively_marketed(status: Optional[str]) -> bool:
    """Return whether DOM may continue advancing for this listing status."""
    normalized = _normalized_status(status)
    return normalized in {"ACTIVE", "ACTIVEBRIGHT", "COMINGSOON", "COMINGSOONBRIGHT"}


def _clean_mls_dom(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def resolve_days_on_market(
    mls_days_on_market,
    status: Optional[str],
    mls_list_date: Optional[DateLike],
    *,
    as_of: Optional[DateLike] = None,
) -> Optional[int]:
    """Resolve DOM without turning elapsed calendar time into active-market time.

    Bright's DaysOnMarket field is authoritative because it accounts for listing-state
    transitions that cannot be reconstructed from a single property snapshot. A date-based
    fallback is allowed only when Bright omitted DOM and the listing is currently market-active.
    """
    explicit_dom = _clean_mls_dom(mls_days_on_market)
    if explicit_dom is not None:
        return explicit_dom
    if not mls_list_date or not is_actively_marketed(status):
        return None

    list_dt = mls_list_date
    if isinstance(list_dt, datetime):
        if list_dt.tzinfo is None:
            list_dt = list_dt.replace(tzinfo=timezone.utc)
        list_day = list_dt.astimezone(timezone.utc).date()
    else:
        list_day = list_dt

    effective_as_of = as_of or datetime.now(timezone.utc)
    if isinstance(effective_as_of, datetime):
        if effective_as_of.tzinfo is None:
            effective_as_of = effective_as_of.replace(tzinfo=timezone.utc)
        as_of_day = effective_as_of.astimezone(timezone.utc).date()
    else:
        as_of_day = effective_as_of
    return max(0, (as_of_day - list_day).days)


def property_days_on_market(property_record, *, as_of: Optional[DateLike] = None) -> Optional[int]:
    """Apply the shared DOM policy to an ORM property or response-shaped object."""
    return resolve_days_on_market(
        getattr(property_record, "days_on_market", None),
        getattr(property_record, "home_status", None),
        getattr(property_record, "mls_list_date", None),
        as_of=as_of,
    )
