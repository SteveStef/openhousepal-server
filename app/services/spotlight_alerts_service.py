"""Listing Spotlight follow-through:

1. Guest alerts - "Tell me about price changes and open houses" on a Spotlight page. The sign-up
   is a real lead for the agent (same lead type as a Spotlight inquiry, so it lands in Leads with
   the normal bell / email / follow-up). Once a day the guest gets one email only when something
   actually changed since they were last told: a new price, a new upcoming open house, or the
   home going under contract / sold. Every email carries a one-click unsubscribe.

2. The agent's weekly Spotlight report (Monday morning) - visitors, visits and the people who
   reached out on each published page in the last 7 days. Skipped when nothing happened.

Both run from app/utils/task_reminder_scheduler.py and claim their day/week through a
system_settings row, so several workers never double-send.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, select, text

from app.config.logging import get_logger
from app.models.database import (
    Lead, LeadEvent, PropertyWebsite, PropertyWebsiteView, SpotlightAlertSubscription, User,
)

logger = get_logger(__name__)

ALERT_HOUR = 10  # Eastern - once a day, after the overnight MLS syncs
WEEKLY_WEEKDAY = 0  # Monday
WEEKLY_HOUR = 8


def _money(v) -> str:
    try:
        return f"${round(float(v)):,}"
    except (TypeError, ValueError):
        return ""


def _status_kind(status: Optional[str]) -> str:
    up = (status or "").upper()
    if "CLOSED" in up or up == "SOLD":
        return "sold"
    if any(k in up for k in ("PENDING", "UNDER CONTRACT", "CONTINGENT", "UNDER_CONTRACT")):
        return "pending"
    return "active"


def _street(site: PropertyWebsite) -> str:
    addr = (site.property_snapshot or {}).get("address") or ""
    return addr.split(",")[0].title() if addr.isupper() else addr.split(",")[0]


def _oh_label(oh: dict) -> str:
    try:
        d = datetime.fromisoformat(str(oh.get("date"))[:10])
        day = f"{d.strftime('%A, %B')} {d.day}"
    except ValueError:
        day = str(oh.get("date") or "")

    def clock(t):
        if not t:
            return ""
        h, m = (int(x) for x in str(t)[:5].split(":"))
        return f"{(h % 12) or 12}{':%02d' % m if m else ''} {'PM' if h >= 12 else 'AM'}"
    times = " - ".join(x for x in (clock(oh.get("start_time")), clock(oh.get("end_time"))) if x)
    return f"{day}{f' · {times}' if times else ''}"


async def _live_state(db, site: PropertyWebsite) -> dict:
    from app.services import property_website_service as pws
    snap = await pws.live_property_snapshot(db, site)
    ohs = await pws.upcoming_open_houses(db, site, site.published_snapshot or {})
    return {"price": snap.get("price"), "status": _status_kind(snap.get("status")), "open_houses": ohs, "snapshot": snap}


async def _agent_bits(db, site: PropertyWebsite) -> dict:
    from app.services.email_avatar_service import agent_email_vars
    """The agent as guests see them on the page (Agent Hub profile: name, phone, photo)."""
    from app.services.website import content_adapters
    owner = await db.get(User, site.owner_id)
    profile = {}
    if owner is not None:
        try:
            profile = await content_adapters.get_agent_profile(db, owner)
        except Exception:  # noqa: BLE001
            profile = {}
    name = profile.get("name") or (f"{getattr(owner, 'first_name', '') or ''} {getattr(owner, 'last_name', '') or ''}".strip() if owner else "")
    phone = "".join(ch for ch in str(profile.get("phone") or "") if ch.isdigit())
    if len(phone) == 11 and phone.startswith("1"):
        phone = phone[1:]
    return {
        "owner": owner, "agent_name": name or "Your agent", "agent_email": profile.get("email") or getattr(owner, "email", None),
        "agent_phone": f"({phone[:3]}) {phone[3:6]}-{phone[6:]}" if len(phone) == 10 else (profile.get("phone") or None),
        "brokerage": profile.get("brokerage"),
        # the small round email photo (never the full-size upload) + title/brokerage
        **({k: v for k, v in (await agent_email_vars(db, owner)).items() if k not in ("agent_name", "agent_email", "agent_phone")} if owner else {}),
    }


# ---------------------------------------------------------------- guest sign-up

async def subscribe(db, site: PropertyWebsite, *, name: str, email: str, came_from: Optional[dict] = None) -> SpotlightAlertSubscription:
    from app.services import leads_service, property_website_service as pws
    from app.services.agent_email_service import email_person
    from app.services.visit_source import clean_came_from
    email = (email or "").strip().lower()
    address = pws._nice_address(site)
    snapshot = {"message": "Wants an email when the price changes or an open house is scheduled.", "property_website_id": site.id, "alerts": True}
    if came_from is not None:
        snapshot["came_from"] = clean_came_from(came_from)
    # Already following this home (tapped it again)? Not news for the agent - no second lead
    # activity, alert or follow-up; they just get their confirmation again.
    existing = (await db.execute(select(SpotlightAlertSubscription).where(
        SpotlightAlertSubscription.property_website_id == site.id, SpotlightAlertSubscription.email == email,
    ))).scalar_one_or_none()
    again = existing is not None and existing.unsubscribed_at is None
    lead = None
    if not again:
        lead = await leads_service.upsert_lead(
            db, owner_id=site.owner_id, name=name, email=email, phone=None,
            source_type="property_website_inquiry", source_id=site.id,
            summary=f"Asked for price & open house alerts on {address}",
            occurred_at=datetime.now(timezone.utc), property_address=address, raw_snapshot=snapshot,
        )
        await pws._notify_owner(
            db, site, lead, title=f"New Spotlight alert sign-up: {name or email}",
            message=f"{name or 'A visitor'} wants price & open house updates for {address}.",
            visitor_name=name, email=email, phone=None, lead_message="Asked to be told about price changes and open houses.",
        )
    live = await _live_state(db, site)
    state = {"status": live["status"], "open_houses": [o.get("id") for o in live["open_houses"]]}
    sub = (await db.execute(select(SpotlightAlertSubscription).where(
        SpotlightAlertSubscription.property_website_id == site.id, SpotlightAlertSubscription.email == email,
    ))).scalar_one_or_none()
    if sub is None:
        sub = SpotlightAlertSubscription(property_website_id=site.id, owner_id=site.owner_id, email=email)
        db.add(sub)
    sub.name = (name or "")[:120] or sub.name
    sub.lead_id = getattr(lead, "id", None) or sub.lead_id
    sub.last_price = live["price"]
    sub.last_state = state
    sub.unsubscribed_at = None  # they asked again
    await db.commit()
    await db.refresh(sub)

    bits = await _agent_bits(db, site)
    url = await pws.public_url(db, site)
    email_person(email, "spotlight_alert_confirm", f"You're on the list for {_street(site)}", {
        "guest_name": (name or "").split(" ")[0] or "there",
        "property_address": _street(site), "city": (site.property_snapshot or {}).get("city", "").title(),
        "hero_image": (site.published_snapshot or {}).get("hero_image") or ((site.property_snapshot or {}).get("photos") or [None])[0],
        "price": _money(live["price"]),
        "next_open_house": _oh_label(live["open_houses"][0]) if live["open_houses"] else None,
        "site_url": url, "unsubscribe_url": _unsubscribe_url(sub.token),
        **{k: v for k, v in bits.items() if k != "owner"},
    }, reply_to=bits["agent_email"], from_name=bits["agent_name"], owner_id=site.owner_id)
    return sub


def _unsubscribe_url(token: str) -> str:
    from app.services.agent_email_service import app_url
    return f"{app_url()}/spotlight-unsubscribe?token={token}"


async def unsubscribe(db, token: str) -> Optional[str]:
    sub = (await db.execute(select(SpotlightAlertSubscription).where(SpotlightAlertSubscription.token == token))).scalar_one_or_none()
    if sub is None:
        return None
    site = await db.get(PropertyWebsite, sub.property_website_id)
    if sub.unsubscribed_at is None:
        sub.unsubscribed_at = datetime.now(timezone.utc)
        await db.commit()
    return _street(site) if site else "this home"


# ---------------------------------------------------------------- daily guest alerts

async def _claim(db, key: str, value: str) -> bool:
    row = (await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, to_jsonb(CAST(:v AS text))) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value WHERE system_settings.value <> EXCLUDED.value "
        "RETURNING key"), {"k": key, "v": value})).first()
    await db.commit()
    return row is not None


def changes_for(sub: SpotlightAlertSubscription, live: dict) -> dict:
    """What this guest hasn't been told yet. Pure - easy to test."""
    state = sub.last_state or {}
    out: dict = {}
    price, old = live.get("price"), sub.last_price
    if price and old and abs(float(price) - float(old)) >= 1:
        out["price"] = {"new": float(price), "old": float(old), "down": float(price) < float(old)}
    if live.get("status") in ("pending", "sold") and state.get("status") != live.get("status"):
        out["status"] = live["status"]
    seen = set(state.get("open_houses") or [])
    fresh = [o for o in live.get("open_houses") or [] if o.get("id") and o["id"] not in seen]
    if fresh and live.get("status") == "active":
        out["open_houses"] = fresh
    return out


async def send_daily_alerts(db, now: Optional[datetime] = None) -> int:
    from app.services import property_website_service as pws
    from app.services.agent_email_service import email_person
    from app.services.tasks_service import EASTERN
    from app.utils.auth import owner_has_access
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(EASTERN)
    if local.hour < ALERT_HOUR or not await _claim(db, "spotlight_alerts_last_run", local.date().isoformat()):
        return 0
    subs = (await db.execute(
        select(SpotlightAlertSubscription, PropertyWebsite)
        .join(PropertyWebsite, PropertyWebsite.id == SpotlightAlertSubscription.property_website_id)
        .where(SpotlightAlertSubscription.unsubscribed_at.is_(None), PropertyWebsite.status == "PUBLISHED")
    )).all()
    sent = 0
    live_cache: dict[str, dict] = {}
    for sub, site in subs:
        try:
            bits = await _agent_bits(db, site)
            if not owner_has_access(bits["owner"], {"IDX"}):
                continue
            if site.id not in live_cache:
                live_cache[site.id] = await _live_state(db, site)
            live = live_cache[site.id]
            news = changes_for(sub, live)
            if news.get("status") == "sold":
                # The agent's branded "This home has sold" email for a home they didn't list or
                # sell would read as their sale (services/sold_branding) - not sent; the guest is
                # still taken off the list below like any sold home.
                from app.services import sold_branding
                if not await sold_branding.branding_allowed_for_listing(db, bits["owner"], site.listing_key):
                    news = {}
            if news:
                price = news.get("price")
                status = news.get("status")
                headline = (
                    "Sold" if status == "sold" else "Under contract" if status == "pending"
                    else f"Price {'reduced' if price['down'] else 'updated'}" if price else "New open house"
                )
                url = await pws.public_url(db, site)
                email_person(sub.email, "spotlight_listing_update", f"{headline}: {_street(site)}", {
                    "guest_name": (sub.name or "").split(" ")[0] or "there",
                    "headline": headline, "property_address": _street(site),
                    "city": (site.property_snapshot or {}).get("city", "").title(),
                    "hero_image": (site.published_snapshot or {}).get("hero_image") or ((site.property_snapshot or {}).get("photos") or [None])[0],
                    "price_changed": bool(price), "new_price": _money(price["new"]) if price else None,
                    "old_price": _money(price["old"]) if price else None, "price_down": bool(price and price["down"]),
                    "price_cut": _money(price["old"] - price["new"]) if price and price["down"] and price.get("old") and price.get("new") else None,
                    "status_line": {"sold": "This home has sold.", "pending": "This home is now under contract."}.get(status or ""),
                    "open_houses": [_oh_label(o) for o in news.get("open_houses", [])],
                    "site_url": url, "unsubscribe_url": _unsubscribe_url(sub.token),
                    **{k: v for k, v in bits.items() if k != "owner"},
                }, reply_to=bits["agent_email"], from_name=bits["agent_name"], owner_id=site.owner_id)
                sent += 1
                sub.last_notified_at = now
            # Always move the "last told" state forward - a change is reported once.
            sub.last_price = live["price"] or sub.last_price
            sub.last_state = {"status": live["status"], "open_houses": [o.get("id") for o in live["open_houses"]]}
            if live["status"] == "sold":
                sub.unsubscribed_at = sub.unsubscribed_at or now  # nothing more to tell them
            await db.commit()
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            await db.rollback()
            logger.warning("spotlight alert failed", exc_info=True, extra={"subscription_id": sub.id})
    logger.info("Spotlight guest alerts", extra={"event": "spotlight_alerts", "sent": sent, "checked": len(subs)})
    return sent


# ---------------------------------------------------------------- weekly agent report

async def weekly_numbers(db, owner_id: str, since: datetime) -> list[dict]:
    from app.services import property_website_service as pws
    sites = (await db.execute(select(PropertyWebsite).where(
        PropertyWebsite.owner_id == owner_id, PropertyWebsite.status == "PUBLISHED",
    ))).scalars().all()
    out = []
    for site in sites:
        visitors, visits = (await db.execute(select(
            func.count(func.distinct(PropertyWebsiteView.visitor_id)), func.count(PropertyWebsiteView.id),
        ).where(PropertyWebsiteView.property_website_id == site.id, PropertyWebsiteView.created_at >= since))).one()
        people = (await db.execute(select(LeadEvent).where(and_(
            LeadEvent.source_id == site.id, LeadEvent.source_type.in_(("property_website_inquiry", "property_website_tour_request")),
            LeadEvent.occurred_at >= since,
        )).order_by(LeadEvent.occurred_at.desc()).limit(10))).scalars().all()
        out.append({
            "site": site, "address": _street(site), "visitors": int(visitors or 0), "visits": int(visits or 0),
            "people": people, "url": await pws.public_url(db, site),
        })
    return out


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


async def send_weekly_reports(db, now: Optional[datetime] = None) -> int:
    from app.services.agent_email_service import email_agent
    from app.services.tasks_service import EASTERN
    from app.utils.auth import owner_has_access
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(EASTERN)
    if local.weekday() != WEEKLY_WEEKDAY or local.hour < WEEKLY_HOUR:
        return 0
    if not await _claim(db, "spotlight_weekly_last_sent", local.date().isoformat()):
        return 0
    since = now - timedelta(days=7)
    owners = (await db.execute(select(User).join(PropertyWebsite, PropertyWebsite.owner_id == User.id)
                               .where(PropertyWebsite.status == "PUBLISHED").distinct())).scalars().all()
    sent = 0
    for user in owners:
        try:
            if not owner_has_access(user, {"IDX"}):
                continue
            rows = await weekly_numbers(db, user.id, since)
            total_visitors = sum(r["visitors"] for r in rows)
            total_visits = sum(r["visits"] for r in rows)
            people = []
            seen_people: set = set()
            for r in rows:
                for ev in r["people"]:
                    if (ev.lead_id, r["address"]) in seen_people:
                        continue  # one line per person per page, however many times they asked
                    seen_people.add((ev.lead_id, r["address"]))
                    lead = await db.get(Lead, ev.lead_id) if ev.lead_id else None
                    who = (lead.name or lead.email) if lead else "A visitor"
                    what = ev.summary.split(" on ")[0].replace("Asked about", "Asked about the home").strip()
                    # the address is shown right beside it - don't say it twice ("tour of 5422 Woodbine Rd · 5422 Woodbine Rd")
                    if r["address"] and r["address"].lower() in what.lower():
                        what = re.sub(r"\s+(?:of|about|for|at)?\s*" + re.escape(r["address"]) + r"\s*$", "", what, flags=re.IGNORECASE).strip() or what
                    people.append({"name": who, "what": what, "address": r["address"], "link": f"/leads/{ev.lead_id}"})
            if total_visits == 0 and not people:
                continue
            rows.sort(key=lambda r: r["visitors"], reverse=True)
            from app.services.agent_email_service import absolute
            email_agent(user, "SPOTLIGHT_WEEKLY", "spotlight_weekly_report",
                        f"Your Spotlight week: {total_visitors} visitor{'s' if total_visitors != 1 else ''}", {
                "week_label": f"{(since.astimezone(EASTERN)).strftime('%b')} {since.astimezone(EASTERN).day} - {local.strftime('%b')} {local.day}",
                "total_visitors": total_visitors, "total_visits": total_visits, "total_people": len(people),
                # ready-made wording so the email never says "1 people visited" / "1 visitors"
                "visitors_label": _count(total_visitors, "person", "people"),
                "sites": [{"address": r["address"], "visitors": r["visitors"], "visits": r["visits"], "people": len(r["people"]), "url": r["url"],
                           "summary": " · ".join([_count(r["visitors"], "visitor", "visitors"), _count(r["visits"], "visit", "visits"),
                                                  f"{len(r['people'])} reached out"])}
                          for r in rows if r["visits"] or r["people"]][:5],
                "sites_more": (f"+ {n} more page{'s' if n != 1 else ''}" if (n := len([r for r in rows if r['visits'] or r['people']]) - 5) > 0 else ""),
                "people": [{**p, "link": absolute(p["link"])} for p in people[:6]],
                "people_more": (f"+ {len(people) - 6} more" if len(people) > 6 else ""),
                "top_address": rows[0]["address"] if rows and rows[0]["visitors"] else None,
                "link": absolute("/studio/property-sites"),
            })
            sent += 1
        except Exception:  # noqa: BLE001
            logger.warning("spotlight weekly report failed", exc_info=True, extra={"user_id": user.id})
    logger.info("Spotlight weekly reports", extra={"event": "spotlight_weekly", "sent": sent})
    return sent
