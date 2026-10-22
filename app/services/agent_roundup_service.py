"""Roll-up emails to the agent - one email for a burst of activity instead of one per event.

- Liked homes: the first like from any client starts a one-hour window; when it closes, ONE email
  covers every home every client liked in that hour ("3 clients liked 5 homes").
- Open house wrap-up: every sign-in is queued, and once TWO hours pass with no new sign-in at that
  open house, ONE email lists all the guests (the in-app bell still alerts on each sign-in live).

Both queue ScheduledEmail rows with their own status (process_due_emails only sends PENDING, so it
never touches them) and are sent by task_reminder_scheduler's every-minute tick. Rows are claimed
with a single UPDATE ... WHERE status = <queued>, so a restart or a second worker can't double-send.
Same approach as the Showcase daily digest (showcase_digest_service)."""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import ScheduledEmail, User

logger = get_logger(__name__)

LIKES_QUEUED, LIKES_SENT = "LIKES_QUEUED", "LIKES_ROLLED"
OH_QUEUED, OH_SENT = "OH_QUEUED", "OH_ROLLED"
LIKES_WINDOW = timedelta(hours=1)    # from the FIRST like in the batch
OH_QUIET = timedelta(hours=2)        # after the LAST sign-in at that open house
LIKES_TEMPLATE = "liked_homes_roundup"
OH_TEMPLATE = "open_house_recap"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _cap_words(name: Optional[str]) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in (name or "").split())


def nice_street(v: Optional[str], city: Optional[str] = None) -> str:
    """MLS streets arrive in capitals ("20-30 W ALLENS LN #105") - "20-30 W Allens Ln #105". Some also
    carry the town, state and ZIP on the end ("846-850  Mt. Pleasant BRYN MAWR PA 19010") - that
    tail and doubled spaces go, so an email subject reads "846-850 Mt. Pleasant"."""
    v = re.sub(r"\s+", " ", (v or "").strip())
    v = re.sub(r"[\s,]+[A-Za-z]{2}[\s,]+\d{5}(?:-\d{4})?$", "", v)       # " PA 19010" / ", PA, 19010"
    if city and v.lower().endswith(" " + city.strip().lower()):
        v = v[: -len(city.strip())].rstrip(" ,")                         # " BRYN MAWR"
    v = re.sub(r"\s*(?:#|apt\.?|unit|ste\.?|suite)\s*$", "", v, flags=re.I)  # an empty unit: "1330 N Avignon Dr #"
    if v and v == v.upper():
        # .title() makes "21ST" "21St" - ordinals stay lower case ("731 S 21st St").
        v = re.sub(r"\b(\d+)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), v.title())
    return v


def _phone(v: Optional[str]) -> str:
    d = "".join(ch for ch in (v or "") if ch.isdigit())
    d = d[1:] if len(d) == 11 and d.startswith("1") else d
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (v or "")


def _group_col():
    # A literal key (not a bound parameter) so GROUP BY and SELECT are the same expression to Postgres.
    return ScheduledEmail.template_variables.op("->>")(literal_column("'_group'"))


def _queue(db: AsyncSession, status: str, to: str, group: str, owner_id: str, template: str, variables: dict) -> None:
    db.add(ScheduledEmail(
        recipient_email=to, subject="(roll-up)", template_name=template, status=status,
        template_variables={**variables, "_group": group, "_owner_id": owner_id, "_seq": time.time_ns()},
        scheduled_for=datetime.now(timezone.utc),
    ))


async def _claim(db: AsyncSession, queued: str, sent: str, ready_groups: list[str]) -> list[dict]:
    if not ready_groups:
        return []
    rows = (await db.execute(
        update(ScheduledEmail)
        .where(ScheduledEmail.status == queued, _group_col().in_(ready_groups))
        .values(status=sent, sent_at=datetime.now(timezone.utc))
        .returning(ScheduledEmail.template_variables, ScheduledEmail.created_at)
    )).all()
    out = []
    for v, created in sorted(rows, key=lambda r: ((r[0] or {}).get("_seq") or 0, r[1])):
        d = dict(v or {})
        d["_at"] = created
        out.append(d)
    return out


# --------------------------------------------------------------------------- liked homes

def queue_like(db: AsyncSession, agent: User, collection, prop, image: Optional[str], price: Optional[float]) -> None:
    """Called when a buyer likes a home in their Showcase (never for the agent's own likes)."""
    if not agent or not agent.email:
        return
    _queue(db, LIKES_QUEUED, agent.email, f"likes:{agent.id}", agent.id, LIKES_TEMPLATE, {
        "_collection_id": collection.id, "_client": _cap_words(collection.visitor_name) or "A client",
        "_listing": str(prop.id), "street_address": nice_street(prop.street_address, prop.city), "city": (prop.city or "").title(),
        "state": prop.state or "", "image": image or "", "price": f"${price:,.0f}" if price else "",
    })


async def send_like_roundups(db: AsyncSession, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    ready = list((await db.execute(
        select(_group_col()).where(ScheduledEmail.status == LIKES_QUEUED)
        .group_by(_group_col()).having(func.min(ScheduledEmail.created_at) <= now - LIKES_WINDOW)
    )).scalars().all())
    rows = await _claim(db, LIKES_QUEUED, LIKES_SENT, ready)
    await db.commit()
    if not rows:
        return 0
    from app.services.agent_email_service import absolute, email_agent

    by_owner: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        by_owner.setdefault(str(r.get("_owner_id")), []).append(r)
    owners = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(list(by_owner))))).scalars().all()}
    sent = 0
    for owner_id, likes in by_owner.items():
        user = owners.get(owner_id)
        if user is None:
            continue
        clients: "OrderedDict[str, dict]" = OrderedDict()
        for r in likes:
            c = clients.setdefault(str(r.get("_collection_id")), {
                "client_name": r.get("_client") or "A client",
                "collection_link": absolute(f"/showcases?showcase={r.get('_collection_id')}"), "homes": [], "_seen": set()})
            if r.get("_listing") in c["_seen"]:  # liked, unliked, liked again - once
                continue
            c["_seen"].add(r.get("_listing"))
            c["homes"].append({k: r.get(k) or "" for k in ("street_address", "city", "state", "image", "price")})
        for c in clients.values():
            c.pop("_seen")
            c["likes_label"] = f"liked {_plural(len(c['homes']), 'home', 'homes')}"
            c["liked_count"] = len(c["homes"])
            extra = len(c["homes"]) - 3          # short email: 3 homes per client, then "+ N more"
            c["homes"] = c["homes"][:3]
            c["more_label"] = f"+ {extra} more" if extra > 0 else ""
        total_homes = sum(c["liked_count"] for c in clients.values())
        if len(clients) == 1:
            only = next(iter(clients.values()))
            headline = f"{only['client_name']} liked {_plural(total_homes, 'home', 'homes')}"
        else:
            headline = f"{len(clients)} clients liked {_plural(total_homes, 'home', 'homes')}"
        email_agent(user, "LIKED_HOMES", LIKES_TEMPLATE, f"{headline} in the last hour", {
            "recipient_name": user.first_name or "there", "headline": headline,
            "clients": list(clients.values())[:4], "dashboard_link": absolute("/showcases"),
            "clients_more": (f"+ {len(clients) - 4} more {'client' if len(clients) - 4 == 1 else 'clients'}" if len(clients) > 4 else ""),
        })
        sent += 1
    logger.info("Liked-homes roll-ups", extra={"event": "likes_roundup", "emails": sent, "likes": len(rows)})
    return sent


# --------------------------------------------------------------------------- open house wrap-up

def queue_sign_in(db: AsyncSession, agent: User, oh, visitor, note: Optional[str], lead_link: Optional[str]) -> None:
    """Called on every open house sign-in. The wrap-up goes out once the sign-ins stop for 2 hours."""
    if not agent or not agent.email:
        return
    has_agent = {"YES": "Has an agent", "NO": "No agent"}.get(str(getattr(visitor, "has_agent", "") or "").upper(), "")
    _queue(db, OH_QUEUED, agent.email, f"oh:{oh.id}", agent.id, OH_TEMPLATE, {
        "_oh_id": oh.id, "_address": nice_street(oh.address) or "your open house", "_city": (oh.city or "").title(),
        "name": _cap_words(visitor.full_name) or "A guest", "email": visitor.email or "", "phone": _phone(visitor.phone),
        "agent_status": has_agent, "no_agent": has_agent == "No agent", "note": note or "", "_lead_link": lead_link or "",
    })


async def send_open_house_recaps(db: AsyncSession, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    ready = list((await db.execute(
        select(_group_col()).where(ScheduledEmail.status == OH_QUEUED)
        .group_by(_group_col()).having(func.max(ScheduledEmail.created_at) <= now - OH_QUIET)
    )).scalars().all())
    rows = await _claim(db, OH_QUEUED, OH_SENT, ready)
    await db.commit()
    if not rows:
        return 0
    from app.services.agent_email_service import absolute, email_agent
    from app.services.tasks_service import EASTERN

    by_oh: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        by_oh.setdefault(str(r.get("_oh_id")), []).append(r)
    owner_ids = {str(r.get("_owner_id")) for r in rows}
    owners = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(list(owner_ids))))).scalars().all()}
    sent = 0
    for oh_id, guests in by_oh.items():
        user = owners.get(str(guests[0].get("_owner_id")))
        if user is None:
            continue
        seen, people = set(), []
        for g in guests:
            key = (g.get("email") or g.get("phone") or g.get("name") or "").lower()
            if key in seen:
                continue
            seen.add(key)
            at = g["_at"].astimezone(EASTERN)
            people.append({k: g.get(k) or "" for k in ("name", "email", "phone", "agent_status", "note")}
                          | {"no_agent": bool(g.get("no_agent")), "time": at.strftime("%I:%M %p").lstrip("0"),
                             "lead_link": absolute(g.get("_lead_link")) if g.get("_lead_link") else ""})
        no_agent = sum(1 for p in people if p["no_agent"])
        first = guests[0]["_at"].astimezone(EASTERN)
        address = guests[0].get("_address")
        email_agent(user, "OPEN_HOUSE_SIGN_IN", OH_TEMPLATE,
                    f"Open house wrap-up: {_plural(len(people), 'guest', 'guests')} at {address}", {
                        "recipient_name": user.first_name or "there", "property_address": address,
                        "city": guests[0].get("_city") or "",
                        "date_label": f"{first.strftime('%A, %B')} {first.day}",
                        "date_short": f"{first.strftime('%b')} {first.day}",
                        "guest_label": _plural(len(people), "guest signed in", "guests signed in"),
                        "no_agent_label": _plural(no_agent, "guest has no agent", "guests have no agent") if no_agent else "",
                        # short email: the first 8 guests (no-agent guests first), then "+ N more"
                        "guests": sorted(people, key=lambda p: not p["no_agent"])[:8],
                        "guests_more": (f"+ {len(people) - 8} more {'guest' if len(people) - 8 == 1 else 'guests'}" if len(people) > 8 else ""),
                        "link": absolute(f"/open-houses/visitors/{oh_id}"),
                        "followups_link": absolute("/leads/tasks"),
                    })
        sent += 1
    logger.info("Open house wrap-ups", extra={"event": "oh_recap", "emails": sent, "sign_ins": len(rows)})
    return sent
