"""Admin Insights -> AI chat (owner, 11/13): how agents use the bottom-right AI helper - how many
chats and messages, roughly how long they spend in it, what it costs, and which features they use
through it (the Confirm cards it made and what they looked up).

Built only from what the chat already records - no new table:
- ai_conversations / ai_messages: who chatted, when (created_at of every message), and the action
  cards (ai_messages.tool_calls entries with record_type "action": the tool and its status).
- ai_usage_events (event_type "chat"): cost per reply and tool_names (the lookups it ran).

Privacy: counts, times and feature names only. Message text and chat titles (made from the agent's
first question) are never read here - agents' questions about their clients are their business.

Time spent is an estimate, because nothing records when the chat window is open: an agent's messages
and the replies are put in time order; a gap of more than 15 minutes starts a new session; a session
lasts from its first message to its last, plus 1 minute for reading the last reply. A session needs
at least one message from the agent (a "your CMA is ready" note posted on its own doesn't count).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import database as m

TZ = ZoneInfo("America/New_York")
PERIODS = (1, 7, 30, 90, 365, 0)
GAP = timedelta(minutes=15)
READING = timedelta(minutes=1)

# Feature groups, in the order the page shows them.
GROUPS = [
    ("design", "Designs & AI media"), ("showcase", "Showcases"), ("homes", "Home search"), ("forms", "Forms & invites"),
    ("followups", "Follow-ups & to-dos"), ("leads", "Leads"), ("open_houses", "Open houses"), ("spotlight", "Spotlight & website"),
    ("cma", "CMAs"), ("blog", "Blog"), ("settings", "Branding & settings"), ("help", "Help & how-to"), ("agents", "Other agents"),
    ("other", "Other"),
]
GROUP_LABEL = dict(GROUPS)

# Reply decorations, not features (a "Go there" button, tap-to-answer chips).
UI_TOOLS = {"show_link", "offer_choices"}

_EXPLICIT = {
    "search_showcase_homes": "homes", "get_new_matches": "showcase", "get_email_tracker_people": "leads",
    "set_client_automatic_emails": "followups", "set_guest_showcase": "showcase", "send_crm_test_lead": "settings",
    "get_feature_details": "help", "make_ai_media": "design",
}
# First match wins - order matters ("resend_showcase_invite" is a Showcase thing, "find_homes_to_host" an open house one).
_KEYWORDS = [
    ("cma", ("cma",)), ("blog", ("blog",)), ("showcase", ("showcase",)), ("forms", ("invite", "form")),
    ("spotlight", ("spotlight", "website", "town")), ("open_houses", ("host", "open_house", "discovery", "guest")),
    ("followups", ("followup", "things_to_do", "priority", "activity_summary", "task")), ("leads", ("lead",)),
    ("design", ("design", "media")), ("homes", ("home", "propert", "listing", "sales", "match")),
    ("settings", ("brand", "profile", "email", "signature", "review", "setup", "account", "usage", "calendar", "activity")),
    ("help", ("help", "ticket")), ("agents", ("agent",)),
]

# What a finished card means, in plain words. Anything not here reads as its tool name.
ACTION_LABELS = {
    "create_design": "Made a design", "create_form": "Made a form", "edit_form": "Changed a form", "duplicate_form": "Copied a form",
    "create_invite": "Made an invite", "duplicate_invite": "Copied an invite", "send_invite_to_leads": "Sent an invite to leads",
    "make_ai_media": "Made an AI photo or video", "start_cma": "Started a CMA", "write_blog_post": "Wrote a blog post",
    "publish_blog_post": "Published a blog post", "create_showcase": "Made a Showcase", "set_showcase": "Changed a Showcase",
    "update_showcase_search": "Changed a Showcase search", "add_home_to_showcase": "Added a home to a Showcase",
    "remove_showcase_homes": "Took homes off a Showcase", "resend_showcase_invite": "Re-sent a Showcase invite",
    "create_followup": "Set a follow-up", "update_followup": "Changed a follow-up", "set_followup_rules": "Changed follow-up rules",
    "set_client_automatic_emails": "Changed a client's automatic emails", "create_spotlight": "Made a Spotlight",
    "publish_spotlight": "Published a Spotlight", "build_open_house_kit": "Built an open house kit",
    "request_to_host": "Asked to host an open house", "request_to_host_several": "Asked to host several homes",
    "set_discovery_search": "Set their Discovery search", "update_branding": "Changed their branding",
    "update_email_settings": "Changed email settings", "update_email_signature": "Changed their email signature",
    "update_profile": "Changed their profile", "update_review_settings": "Changed review settings",
    "add_lead_note": "Added a note to a lead", "update_lead": "Changed a lead", "hide_lead": "Hid a lead",
    "add_town": "Added a town page", "open_help_ticket": "Opened a help ticket", "reply_to_help_ticket": "Replied to a help ticket",
    "set_website_on": "Turned their website on or off", "create_form_tracking_link": "Made a form tracking link",
}

# Card status -> what happened. done/working = it ran (working: an AI CMA or blog still finishing).
_STATE = {"done": "done", "working": "done", "failed": "failed", "stale": "failed",
          "pending": "skipped", "expired": "skipped", "cancelled": "skipped", "replaced": "skipped", "running": "open"}


def tool_group(name: str) -> str:
    n = (name or "").lower()
    if n in _EXPLICIT:
        return _EXPLICIT[n]
    for key, words in _KEYWORDS:
        if any(w in n for w in words):
            return key
    return "other"


def action_label(name: str) -> str:
    if name in ACTION_LABELS:
        return ACTION_LABELS[name]
    words = (name or "something").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def card_state(status: Optional[str]) -> str:
    return _STATE.get((status or "").lower(), "skipped")


# ---------------------------------------------------------------- rows (what the database gives us)

@dataclass
class Msg:
    at: datetime
    role: str
    conv: str
    owner: str


@dataclass
class Card:
    at: datetime
    owner: str
    conv: str
    tool: str
    status: Optional[str]


@dataclass
class Turn:
    at: datetime
    owner: Optional[str]
    conv: Optional[str]
    tools: list
    cost: float
    blocked: bool


@dataclass
class Session:
    start: datetime
    end: datetime
    messages: int        # the agent's own messages in it
    replies: int

    @property
    def seconds(self) -> int:
        return int((self.end - self.start + READING).total_seconds())


def split_sessions(msgs: Iterable[Msg]) -> list[Session]:
    """Messages (any order) -> sessions: a gap of more than 15 minutes starts a new one; sessions
    without a message from the agent are dropped."""
    out: list[Session] = []
    cur: Optional[Session] = None
    for x in sorted(msgs, key=lambda x: x.at):
        if cur is None or x.at - cur.end > GAP:
            if cur is not None and cur.messages:
                out.append(cur)
            cur = Session(x.at, x.at, 0, 0)
        cur.end = x.at
        if x.role == "user":
            cur.messages += 1
        else:
            cur.replies += 1
    if cur is not None and cur.messages:
        out.append(cur)
    return out


def _median(xs: list) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    k = len(s) // 2
    return float(s[k]) if len(s) % 2 else (s[k - 1] + s[k]) / 2


def _pct(a: float, b: float) -> Optional[float]:
    return round(100.0 * a / b, 1) if b else None


def _day(at: datetime) -> date:
    return at.astimezone(TZ).date()


def unit_for(days: int) -> str:
    return "day" if days and days <= 90 else "week"


def bucket_of(at: datetime, unit: str) -> date:
    d = _day(at)
    return d - timedelta(days=d.weekday()) if unit == "week" else d


def buckets(days: int, now: datetime, first: Optional[date]) -> list[date]:
    unit = unit_for(days)
    today = _day(now)
    start = (today - timedelta(days=days - 1)) if days else (first or today)
    if unit == "week":
        start -= timedelta(days=start.weekday())
    out, d = [], start
    while d <= today:
        out.append(d)
        d += timedelta(days=1 if unit == "day" else 7)
    return out


@dataclass
class Usage:
    """One agent's (or everyone's) chat use over a period - see summarize()."""
    msgs: list = field(default_factory=list)
    cards: list = field(default_factory=list)
    turns: list = field(default_factory=list)


def _features(cards: list[Card], turns: list[Turn], action_names: set[str]) -> tuple[list[dict], list[dict], int]:
    """(groups, actions, lookups). Groups: per feature group, cards made / done and lookups run.
    A tool counts as a lookup when it isn't an action (cards are counted from the cards themselves)."""
    g: dict[str, dict] = {k: {"key": k, "label": lbl, "made": 0, "done": 0, "lookups": 0, "agents": set()} for k, lbl in GROUPS}
    acts: dict[str, dict] = {}
    for c in cards:
        st = card_state(c.status)
        row = g[tool_group(c.tool)]
        row["made"] += 1
        row["agents"].add(c.owner)
        a = acts.setdefault(c.tool, {"tool": c.tool, "label": action_label(c.tool), "group": tool_group(c.tool),
                                     "group_label": GROUP_LABEL[tool_group(c.tool)],
                                     "made": 0, "done": 0, "skipped": 0, "failed": 0, "open": 0, "agents": set()})
        a["made"] += 1
        a[st] += 1
        a["agents"].add(c.owner)
        if st == "done":
            row["done"] += 1
    lookups = 0
    for t in turns:
        for name in t.tools or []:
            if not isinstance(name, str) or name in UI_TOOLS or name in action_names:
                continue
            row = g[tool_group(name)]
            row["lookups"] += 1
            if t.owner:
                row["agents"].add(t.owner)
            lookups += 1
    groups = [{**r, "agents": len(r["agents"])} for r in g.values() if r["made"] or r["lookups"]]
    groups.sort(key=lambda r: (-(r["done"] + r["lookups"]), -r["made"]))
    actions = [{**a, "agents": len(a["agents"])} for a in acts.values()]
    actions.sort(key=lambda r: (-r["done"], -r["made"], r["label"]))
    return groups, actions, lookups


def summarize(u: Usage, action_names: set[str], sessions: Optional[list] = None) -> dict:
    """The numbers for one agent (or a set of agents) - pure, so it can be tested without a database."""
    # Several agents together: pass each agent's own sessions (their timelines must never merge).
    sessions = split_sessions(u.msgs) if sessions is None else sessions
    secs = [s.seconds for s in sessions]
    mine = [x for x in u.msgs if x.role == "user"]
    groups, actions, lookups = _features(u.cards, u.turns, action_names)
    chat_cost = round(sum(t.cost or 0.0 for t in u.turns), 6)
    return {
        "chats": len({x.conv for x in mine}),
        "messages": len(mine),
        "replies": sum(1 for x in u.msgs if x.role != "user"),
        "sessions": len(sessions),
        "seconds": sum(secs),
        "avg_session_seconds": round(sum(secs) / len(secs)) if secs else None,
        "median_session_seconds": _median(secs),
        "longest_session_seconds": max(secs) if secs else None,
        "active_days": len({_day(x.at) for x in mine}),
        "cost_usd": chat_cost,
        "cards_made": len(u.cards),
        "things_done": sum(1 for c in u.cards if card_state(c.status) == "done"),
        "lookups": lookups,
        "blocked": sum(1 for t in u.turns if t.blocked),
        "groups": groups,
        "actions": actions,
        "_sessions": sessions,
    }


def hours_of(msgs: Iterable[Msg]) -> list[int]:
    h = [0] * 24
    for x in msgs:
        if x.role == "user":
            h[x.at.astimezone(TZ).hour] += 1
    return h


def series_of(msgs: Iterable[Msg], days: int, now: datetime) -> list[dict]:
    unit = unit_for(days)
    mine = [x for x in msgs if x.role == "user"]
    first = min((_day(x.at) for x in mine), default=None)
    per: dict[date, dict] = defaultdict(lambda: {"messages": 0, "chats": set(), "agents": set()})
    for x in mine:
        b = per[bucket_of(x.at, unit)]
        b["messages"] += 1
        b["chats"].add(x.conv)
        b["agents"].add(x.owner)
    return [{"d": d.isoformat(), "messages": per[d]["messages"] if d in per else 0,
             "chats": len(per[d]["chats"]) if d in per else 0, "agents": len(per[d]["agents"]) if d in per else 0}
            for d in buckets(days, now, first)]


def conversations_of(u: Usage, convs: dict[str, dict], action_names: set[str], limit: int = 30) -> list[dict]:
    """The agent's chats with activity in the period, newest first - counts, time and features, no text."""
    by: dict[str, Usage] = defaultdict(Usage)
    for x in u.msgs:
        by[x.conv].msgs.append(x)
    for c in u.cards:
        by[c.conv].cards.append(c)
    for t in u.turns:
        if t.conv:
            by[t.conv].turns.append(t)
    rows = []
    for cid, cu in by.items():
        if not any(x.role == "user" for x in cu.msgs):
            continue
        s = summarize(cu, action_names)
        meta = convs.get(cid) or {}
        rows.append({
            "id": cid, "started_at": meta.get("created_at"), "last_at": max(x.at for x in cu.msgs),
            "deleted_by_agent": bool(meta.get("archived_at")),
            "messages": s["messages"], "replies": s["replies"], "sessions": s["sessions"], "seconds": s["seconds"],
            "cost_usd": s["cost_usd"], "things_done": s["things_done"], "cards_made": s["cards_made"], "lookups": s["lookups"],
            "cards": [{"label": action_label(c.tool), "group": GROUP_LABEL[tool_group(c.tool)], "state": card_state(c.status)}
                      for c in sorted(cu.cards, key=lambda c: c.at)],
            "looked_up": [g["label"] for g in s["groups"] if g["lookups"]],
        })
    rows.sort(key=lambda r: r["last_at"], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------- database

def _action_names() -> set[str]:
    try:
        from app.services.ai import ai_actions
        ai_actions._load()
        return set(ai_actions.ACTIONS)
    except Exception:  # noqa: BLE001 - only used to tell a card tool from a lookup
        return set(ACTION_LABELS)


async def _load(db: AsyncSession, since: Optional[datetime], owner_id: Optional[str] = None) -> tuple[dict[str, Usage], dict[str, dict]]:
    from app.api.admin_ai_routes import _real_cost
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    C, M_, E = m.AIConversation, m.AIMessage, m.AIUsageEvent
    per: dict[str, Usage] = defaultdict(Usage)
    convs: dict[str, dict] = {}

    q = (select(M_.created_at, M_.role, M_.conversation_id, C.owner_id, C.created_at, C.archived_at)
         .join(C, C.id == M_.conversation_id)
         .where(M_.created_at >= since if since else true()))
    if owner_id:
        q = q.where(C.owner_id == owner_id)
    for at, role, conv, owner, c_at, c_arch in (await db.execute(q)).all():
        per[owner].msgs.append(Msg(at, role, conv, owner))
        convs.setdefault(conv, {"created_at": c_at, "archived_at": c_arch})

    sql = ("SELECT msg.created_at, conv.owner_id, msg.conversation_id, e->>'tool', e->>'status' "
           "FROM ai_messages msg JOIN ai_conversations conv ON conv.id = msg.conversation_id "
           "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(msg.tool_calls) = 'array' THEN msg.tool_calls ELSE '[]'::jsonb END) e "
           "WHERE e->>'record_type' = 'action'")
    params: dict[str, Any] = {}
    if since:
        sql += " AND msg.created_at >= :since"
        params["since"] = since
    if owner_id:
        sql += " AND conv.owner_id = :owner"
        params["owner"] = owner_id
    for at, owner, conv, tool, status in (await db.execute(text(sql), params)).all():
        per[owner].cards.append(Card(at, owner, conv, tool or "unknown", status))

    q = (select(E.created_at, E.owner_id, E.conversation_id, E.tool_names, _real_cost(config), E.blocked, E.block_reason)
         .where(E.event_type == "chat", E.owner_id.isnot(None), E.created_at >= since if since else true()))
    if owner_id:
        q = q.where(E.owner_id == owner_id)
    for at, owner, conv, tools, cost, blocked, reason in (await db.execute(q)).all():
        # A refunded run isn't a block (admin_ai_routes._a_real_block).
        per[owner].turns.append(Turn(at, owner, conv, tools if isinstance(tools, list) else [], float(cost or 0.0),
                                     bool(blocked) and reason != "refunded_failed_run"))
    return per, convs


async def _ai_cost_by_owner(db: AsyncSession, since: Optional[datetime], owner_id: Optional[str] = None) -> dict[str, float]:
    from app.api.admin_ai_routes import _spent
    from app.services.ai.ai_config_service import ai_config_service
    config = await ai_config_service.get_config(db)
    E = m.AIUsageEvent
    q = select(E.owner_id, _spent(config)).where(E.owner_id.isnot(None), E.created_at >= since if since else true()).group_by(E.owner_id)
    if owner_id:
        q = q.where(E.owner_id == owner_id)
    return {o: float(c or 0.0) for o, c in (await db.execute(q)).all()}


async def _last_used(db: AsyncSession, owner_id: Optional[str] = None) -> dict[str, datetime]:
    C, M_ = m.AIConversation, m.AIMessage
    q = (select(C.owner_id, func.max(M_.created_at)).join(C, C.id == M_.conversation_id)
         .where(M_.role == "user").group_by(C.owner_id))
    if owner_id:
        q = q.where(C.owner_id == owner_id)
    return dict((await db.execute(q)).all())


async def _messages_between(db: AsyncSession, start: datetime, end: datetime, ids: set[str]) -> tuple[int, int]:
    C, M_ = m.AIConversation, m.AIMessage
    n, agents = (await db.execute(
        select(func.count(), func.count(func.distinct(C.owner_id))).select_from(M_).join(C, C.id == M_.conversation_id)
        .where(M_.role == "user", M_.created_at >= start, M_.created_at < end, C.owner_id.in_(ids) if ids else true())
    )).one()
    return int(n or 0), int(agents or 0)


def _name(u) -> str:
    return f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email


def _row(u, s: dict, ai_cost: float, last: Optional[datetime]) -> dict:
    top = [g["label"] for g in s["groups"] if g["done"] or g["lookups"]][:3]
    return {
        "id": u.id, "name": _name(u), "plan_tier": u.plan_tier, "subscription_status": u.subscription_status,
        **{k: v for k, v in s.items() if not k.startswith("_") and k not in ("groups", "actions")},
        "ai_cost_usd": round(ai_cost, 6), "ai_share_pct": _pct(s["cost_usd"], ai_cost), "last_used_at": last, "top_features": top,
    }


def _meta(days: int, now: datetime) -> dict:
    return {"days": days, "unit": unit_for(days), "generated_at": now,
            "gap_minutes": int(GAP.total_seconds() // 60), "reading_seconds": int(READING.total_seconds())}


async def overview(db: AsyncSession, days: int, now: Optional[datetime] = None) -> dict:
    from app.services import admin_activity_service as act
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
    agents = {u.id: u for u in await act._agents(db)}
    names = _action_names()
    per, _ = await _load(db, since)
    per = {o: u for o, u in per.items() if o in agents}
    ai_cost = await _ai_cost_by_owner(db, since)
    last = await _last_used(db)

    everyone = Usage()
    all_sessions: list = []
    rows = []
    for uid, u in agents.items():
        mine = per.get(uid) or Usage()
        everyone.msgs += mine.msgs
        everyone.cards += mine.cards
        everyone.turns += mine.turns
        one = summarize(mine, names)
        all_sessions += one["_sessions"]
        rows.append(_row(u, one, ai_cost.get(uid, 0.0), last.get(uid)))
    rows.sort(key=lambda r: (-r["messages"], -r["seconds"], r["name"]))

    s = summarize(everyone, names, sessions=all_sessions)
    chatted = sum(1 for r in rows if r["messages"])
    prev = await _messages_between(db, since - timedelta(days=days), since, set(agents)) if since else None
    agents_ai = sum(ai_cost.get(uid, 0.0) for uid in agents)
    kpis = {k: v for k, v in s.items() if not k.startswith("_") and k not in ("groups", "actions")}
    kpis.update({
        "agents_total": len(agents), "agents_chatted": chatted, "agents_chatted_pct": _pct(chatted, len(agents)),
        "ai_cost_usd": round(agents_ai, 6), "ai_share_pct": _pct(s["cost_usd"], agents_ai),
        "cost_per_message_usd": round(s["cost_usd"] / s["messages"], 6) if s["messages"] else None,
        "prev_messages": prev[0] if prev else None, "prev_agents_chatted": prev[1] if prev else None,
    })
    return {**_meta(days, now), "kpis": kpis, "series": series_of(everyone.msgs, days, now), "hours": hours_of(everyone.msgs),
            "groups": s["groups"], "actions": s["actions"], "agents": rows}


async def agent_detail(db: AsyncSession, agent_id: str, days: int, now: Optional[datetime] = None) -> Optional[dict]:
    now = now or datetime.now(timezone.utc)
    u = await db.get(m.User, agent_id)
    if u is None:
        return None
    since = now - timedelta(days=days) if days else None
    names = _action_names()
    per, convs = await _load(db, since, owner_id=agent_id)
    mine = per.get(agent_id) or Usage()
    s = summarize(mine, names)
    ai_cost = (await _ai_cost_by_owner(db, since, owner_id=agent_id)).get(agent_id, 0.0)
    last = (await _last_used(db, owner_id=agent_id)).get(agent_id)
    allowance = await _allowance(db, u)
    first_ever = (await db.execute(
        select(func.min(m.AIMessage.created_at)).join(m.AIConversation, m.AIConversation.id == m.AIMessage.conversation_id)
        .where(m.AIConversation.owner_id == agent_id, m.AIMessage.role == "user"))).scalar()
    return {
        **_meta(days, now), **_row(u, s, ai_cost, last), "first_used_at": first_ever, "allowance": allowance,
        "series": series_of(mine.msgs, days, now), "hours": hours_of(mine.msgs), "groups": s["groups"], "actions": s["actions"],
        "recent_sessions": [{"start": x.start, "end": x.end, "seconds": x.seconds, "messages": x.messages, "replies": x.replies}
                            for x in sorted(s["_sessions"], key=lambda x: x.start, reverse=True)[:15]],
        "conversations": conversations_of(mine, convs, names),
    }


async def _allowance(db: AsyncSession, u) -> Optional[dict]:
    """Chat's share of this agent's AI Usage allowance this billing period (chat counts on the one
    AI bar since 11/13). Read-only use of the rate-limit helpers; None when the plan isn't gated."""
    try:
        from app.api.admin_ai_routes import _spent
        from app.services.ai.ai_config_service import ai_config_service
        from app.services.ai.ai_rate_limit_service import monthly_limit_usd, usage_period
        config = await ai_config_service.get_config(db)
        limit = await monthly_limit_usd(db, u.id, config, u.plan_tier)
        start, end = await usage_period(db, u.id)
        E = m.AIUsageEvent
        chat = (await db.execute(select(_spent(config, E.event_type == "chat")).where(E.owner_id == u.id, E.created_at >= start))).scalar()
        return {"period_start": start, "period_end": end, "limit_usd": limit, "chat_cost_usd": round(float(chat or 0.0), 6),
                "chat_pct_of_allowance": _pct(float(chat or 0.0), limit) if limit else None}
    except Exception:  # noqa: BLE001 - a nicety on the detail sheet, never a reason to fail it
        return None
