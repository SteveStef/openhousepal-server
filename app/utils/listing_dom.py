from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo


DateLike = Union[date, datetime]
_ET = ZoneInfo("America/New_York")


def _normalized_status(status: Optional[str]) -> str:
    return "".join(ch for ch in str(status or "").upper() if ch.isalnum())


def is_actively_marketed(status: Optional[str]) -> bool:
    """Return whether DOM may continue advancing for this listing status."""
    normalized = _normalized_status(status)
    return normalized in {"ACTIVE", "ACTIVEBRIGHT", "COMINGSOON", "COMINGSOONBRIGHT"}


def _counts_daily(status: Optional[str]) -> bool:
    """Bright's DaysOnMarket goes up by one every day a listing is Active (not while Coming Soon,
    and it stops once it's under contract)."""
    return _normalized_status(status) in {"ACTIVE", "ACTIVEBRIGHT"}


def _clean_mls_dom(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _day(value) -> Optional[date]:
    """A date in Bright's own (Eastern) calendar - datetimes, dates and ISO strings."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_ET).date()
    if isinstance(value, date):
        return value
    return None


def resolve_days_on_market(
    mls_days_on_market,
    status: Optional[str],
    mls_list_date: Optional[DateLike],
    *,
    as_of: Optional[DateLike] = None,
    fetched_at=None,
) -> Optional[int]:
    """Days on market as of today (or `as_of`).

    Bright's DaysOnMarket is authoritative (it accounts for off-market periods and relists that a
    list date can't show) - but it's the count on the day WE read the listing (`fetched_at`), and
    Bright only re-sends a listing when it changes. Shown as stored, an active home's DOM froze:
    on the live site every active listing was behind, typically by 4 days and sometimes by weeks
    (user, 10/19 - "our DOM is off by 2-3 days"). So while it's Active, the days since we read it
    are added - the same count Bright itself shows today. A date-based fallback is used only when
    Bright omitted DOM and the listing is currently market-active.
    """
    explicit_dom = _clean_mls_dom(mls_days_on_market)
    as_of_day = _day(as_of or datetime.now(timezone.utc))
    if explicit_dom is not None:
        read_day = _day(fetched_at)
        if read_day and as_of_day and _counts_daily(status) and as_of_day > read_day:
            return explicit_dom + (as_of_day - read_day).days
        return explicit_dom
    if not mls_list_date or not is_actively_marketed(status):
        return None
    list_day = _day(mls_list_date)
    if not list_day or not as_of_day:
        return None
    return max(0, (as_of_day - list_day).days)


def dom_fetched_at(property_record):
    """When this home's DaysOnMarket was read from Bright: the sync stamps updated_at on every write
    of Bright's data (80% of active listings match it to the day; the rest are within a day)."""
    return getattr(property_record, "updated_at", None)


def property_days_on_market(property_record, *, as_of: Optional[DateLike] = None) -> Optional[int]:
    """Apply the shared DOM policy to an ORM property or response-shaped object."""
    return resolve_days_on_market(
        getattr(property_record, "days_on_market", None),
        getattr(property_record, "home_status", None),
        getattr(property_record, "mls_list_date", None),
        as_of=as_of,
        fetched_at=dom_fetched_at(property_record),
    )
