""""Recent" sales wording (user rule, 10/18): an agent's sales are called RECENT only while the total
volume of the sales shown is under $50M - for a smaller agent "recent" explains a modest count;
past that, plain "Sales" reads truer. Same rule on the Agent Hub, the IDX website, the Brag Sheet
and the email signature. Frontend twin: lib/salesWording.ts.
"""
from __future__ import annotations

from typing import Optional

RECENT_SALES_VOLUME_CAP = 50_000_000

_DROP_RECENT = {
    "My Recent Sales": "My Sales",
    "Recent Sales": "Sales",
    "Recent Sales Figures": "Sales Figures",
    "Recently Sold": "Sold",
    "Recently sold": "Sold",
    "Recent sale": "Sold",
    "RECENT SALE": "SOLD",
    "RECENTLY SOLD": "SOLD",
}


def sales_are_recent(total_volume: Optional[float]) -> bool:
    return (total_volume or 0) < RECENT_SALES_VOLUME_CAP


def sales_words(text: str, recent: bool) -> str:
    """`text` as written while the sales count as recent, without "recent" once they don't."""
    return text if recent else _DROP_RECENT.get(text, text)
