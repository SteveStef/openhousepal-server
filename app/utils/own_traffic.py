"""The agent's own traffic on their own public pages (owner, 11/13): "no stats on the site should
count the signed-in agent - open house sign-in views and completion, forms, Agent Hub... but should
still count everyone else."

One rule everywhere a public page records a view / scan / start / submission:
- `is_own_traffic(viewer, owner_id)`: the signed-in caller (the optional token the page sends) is
  the agent who owns the page - or the site admin - so the hit isn't a real guest.
- `contact_is_owner(owner, email)`: a submission typed with the agent's own account email is the
  agent testing, signed in or not (a kiosk tablet, their own site on another domain...).

Pure views are simply not recorded. Submissions are still saved (and still work end to end) but
carry `owner_test = True`, which every stat query leaves out (`not_owner_test(...)`).
"""
import os
from typing import Any, Optional


def _admin_email() -> str:
    return (os.getenv("ADMIN_EMAIL", "admin@openhousepal.com") or "").strip().lower()


def is_own_traffic(viewer: Any, owner_id: Optional[str]) -> bool:
    """True when the signed-in viewer is this page's owner (or the site admin)."""
    if viewer is None:
        return False
    if owner_id and getattr(viewer, "id", None) == owner_id:
        return True
    return (getattr(viewer, "email", "") or "").strip().lower() == _admin_email()


def contact_is_owner(owner: Any, email: Optional[str]) -> bool:
    """A submission made with the owner's own account email."""
    owner_email = (getattr(owner, "email", "") or "").strip().lower() if owner is not None else ""
    return bool(owner_email) and (email or "").strip().lower() == owner_email


def not_owner_test(column):
    """SQL filter: rows that aren't the agent's own test (NULL = an ordinary guest)."""
    return column.isnot(True)
