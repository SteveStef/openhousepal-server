"""Where an agent's client reviews show, and which ones - one reading of AgentHub.config["reviews"]
shared by the Agent Hub (agent_hub_routes) and the IDX website (public_site_routes, content_adapters).

  showOnHub  - the Agent Hub's Reviews section / card (default off)
  showOnIdx  - the IDX website's Reviews page, its menu link and any Client Reviews section (default off)
  minRating  - 4 = only 4 and 5 star reviews are shown anywhere public; 1 (default) = every published one
"""


def review_prefs(hub_config: dict | None) -> dict:
    r = (hub_config or {}).get("reviews") or {}
    try:
        min_rating = int(r.get("minRating") or 1)
    except (TypeError, ValueError):
        min_rating = 1
    return {
        "show_on_hub": bool(r.get("showOnHub")),
        "show_on_idx": bool(r.get("showOnIdx")),
        "min_rating": 4 if min_rating >= 4 else 1,
    }


def shown_reviews(rows: list, min_rating: int) -> list:
    """Published reviews at or above the agent's minimum star rating, in the order given."""
    return [rv for rv in rows if rv.status == "published" and (rv.rating or 0) >= min_rating]


def summarize(rows: list) -> dict:
    if not rows:
        return {"count": 0, "average": None}
    return {"count": len(rows), "average": round(sum(rv.rating for rv in rows) / len(rows), 2)}
