"""The agent's two non-mobile numbers, kept apart (user, 10/16):

- Brokerage phone (profile.brokeragePhone) - the brokerage's main number. Required, and printed on
  designs for advertising compliance. Never put on the Save Contact card.
- Office phone (contactCard.fields.officePhone) - the agent's OWN office / direct line. Optional;
  it's what Save Contact lists as "work".

Before 10/16 there was one field, labelled "Brokerage office phone", stored as officePhone and
also put on Save Contact. A config without a brokeragePhone key is that older shape: its
officePhone is the brokerage number, so it counts as the brokerage phone and NOT as an office line.
"""
from typing import Any


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _office_value(cfg: dict) -> str:
    field = (((cfg or {}).get("contactCard") or {}).get("fields") or {}).get("officePhone")
    return _s(field.get("value")) if isinstance(field, dict) else ""


def is_legacy(cfg: dict) -> bool:
    return "brokeragePhone" not in (((cfg or {}).get("profile")) or {})


def brokerage_phone(cfg: dict) -> str:
    if is_legacy(cfg):
        return _office_value(cfg)
    return _s(((cfg or {}).get("profile") or {}).get("brokeragePhone"))


def office_phone(cfg: dict) -> str:
    """The agent's own office line - empty for an older config (its number was the brokerage's)."""
    return "" if is_legacy(cfg) else _office_value(cfg)


def upgrade(cfg: dict) -> dict:
    """Move an older config to the two-number shape (returns a new dict; no-op when already moved)."""
    if not is_legacy(cfg):
        return cfg
    cfg = dict(cfg or {})
    profile = dict(cfg.get("profile") or {})
    profile["brokeragePhone"] = _office_value(cfg)
    cfg["profile"] = profile
    card = dict(cfg.get("contactCard") or {})
    fields = dict(card.get("fields") or {})
    if isinstance(fields.get("officePhone"), dict):
        fields["officePhone"] = {**fields["officePhone"], "value": "", "include": False}
        card["fields"] = fields
        cfg["contactCard"] = card
    return cfg
