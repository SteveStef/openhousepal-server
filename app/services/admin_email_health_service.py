"""Admin Insights -> Email health (user, 10/24 - "our sender score is my main concern"): for a day or a
date range, every email we sent, who it went to (agents / their clients / account emails), which
were opened, which kinds go unopened and which are repetitive, agent by agent.

Same privacy rule as the rest of Insights: agents are named, their clients never are (a client is
only ever "a client of <agent>"). Reads our own ledger (email_log) + client_emails - no Mailgun calls.
Times are Eastern."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import database as m
from app.services import agent_email_health_service as health
from app.services.admin_insights_service import TEMPLATE_LABELS, _agent_map, _agent_row, _eastern, pct

MAX_DAYS = 92
# "Repetitive": the same person got the same kind of email this often.
REPEAT_DAY = 3      # in any 24 hours
REPEAT_WEEK = 6     # in any 7 days
BAD = ("bounced", "blocked")


def _label(t: str) -> str:
    return TEMPLATE_LABELS.get(t) or health.LABELS.get(t) or t.replace("_v2", "").replace("_", " ").capitalize()


def parse_range(start: Optional[str], end: Optional[str]) -> tuple[date, date]:
    today = datetime.now(_eastern()).date()
    try:
        s = date.fromisoformat(start) if start else today
    except ValueError:
        s = today
    try:
        e = date.fromisoformat(end) if end else s
    except ValueError:
        e = s
    if e < s:
        s, e = e, s
    e = min(e, today)
    s = min(s, e)
    if (e - s).days >= MAX_DAYS:
        s = e - timedelta(days=MAX_DAYS - 1)
    return s, e


def _bounds(s: date, e: date) -> tuple[datetime, datetime]:
    tz = _eastern()
    return (datetime.combine(s, datetime.min.time(), tz).astimezone(timezone.utc),
            datetime.combine(e + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc))


async def _emails(db: AsyncSession, lo: datetime, hi: datetime, owner_id: Optional[str] = None) -> list[dict]:
    """Every email in the window as one shape: {template, audience, status, delivery, owner, at, who,
    tracked, opened, clicked}. `who` is who it went to (agent id / client address key) - never shown."""
    from app.services import email_log_service
    await email_log_service.flush()
    G, CE = m.EmailLog, m.ClientEmail
    q = select(G.template, G.audience, G.status, G.delivery, G.owner_id, G.client_email_id, G.created_at, G.tracked,
               G.opened_at, G.clicked_at, G.via).where(G.created_at >= lo, G.created_at < hi)
    cq = select(CE.id, CE.owner_id, CE.template, CE.recipient_email, CE.status, CE.delivery, CE.via,
                CE.sent_at, CE.created_at, CE.opened_at, CE.clicked_at).where(
        CE.status.in_(("sent", "failed")), CE.created_at >= lo - timedelta(days=1), CE.created_at < hi)
    if owner_id:
        q, cq = q.where(G.owner_id == owner_id), cq.where(CE.owner_id == owner_id)
    log = (await db.execute(q)).all()
    ce = {r.id: r for r in (await db.execute(cq)).all()}
    missing = [r.client_email_id for r in log if r.client_email_id and r.client_email_id not in ce]
    if missing:
        for r in (await db.execute(select(CE.id, CE.owner_id, CE.template, CE.recipient_email, CE.status, CE.delivery, CE.via, CE.sent_at,
                                          CE.created_at, CE.opened_at, CE.clicked_at).where(CE.id.in_(missing)))).all():
            ce[r.id] = r
    out, logged = [], set()
    for r in log:
        c = ce.get(r.client_email_id) if r.client_email_id else None
        if c is not None:
            logged.add(c.id)
            out.append({"template": r.template, "audience": "client", "status": r.status, "delivery": r.delivery or c.delivery, "owner": r.owner_id,
                        "at": r.created_at, "who": f"c:{c.owner_id}:{c.recipient_email}", "tracked": c.status == "sent",
                        "opened": bool(c.opened_at or c.clicked_at), "clicked": bool(c.clicked_at)})
        else:
            agent = r.audience == "agent"
            out.append({"template": r.template, "audience": r.audience, "status": "repeat" if r.via == "dedupe" else r.status, "delivery": r.delivery, "owner": r.owner_id,
                        "at": r.created_at, "who": f"a:{r.owner_id}" if agent and r.owner_id else None, "tracked": bool(agent and r.tracked),
                        "opened": bool(r.opened_at or r.clicked_at), "clicked": bool(r.clicked_at)})
    # client emails from before everything was logged / sent from the agent's own mailbox before then
    for c in ce.values():
        at = c.sent_at or c.created_at
        if c.id in logged or not (lo <= at < hi):
            continue
        out.append({"template": c.template, "audience": "client", "status": c.status, "delivery": c.delivery, "owner": c.owner_id, "at": at,
                    "who": f"c:{c.owner_id}:{c.recipient_email}", "tracked": c.status == "sent",
                    "opened": bool(c.opened_at or c.clicked_at), "clicked": bool(c.clicked_at)})
    return out


def _tally(rows: list[dict]) -> dict:
    sent = [r for r in rows if r["status"] not in ("failed", "repeat")]
    tracked = [r for r in sent if r["tracked"]]
    opened = sum(1 for r in tracked if r["opened"])
    clicked = sum(1 for r in tracked if r["clicked"])
    bounced = sum(1 for r in rows if r["delivery"] in BAD)
    spam = sum(1 for r in rows if r["delivery"] == "spam")
    return {"sent": len(sent), "failed": sum(1 for r in rows if r["status"] == "failed"), "repeats_held": sum(1 for r in rows if r["status"] == "repeat"), "tracked": len(tracked), "opened": opened,
            "unopened": len(tracked) - opened, "clicked": clicked, "bounced": bounced, "spam": spam,
            "held": sum(1 for r in rows if r["status"] == "held"),
            "open_rate": pct(opened, len(tracked)), "click_rate": pct(clicked, len(tracked)),
            "bounce_rate": pct(bounced, len(rows)), "spam_rate": pct(spam, len(rows))}


def _peak(times: list[datetime], window: timedelta) -> int:
    best, j = 0, 0
    for i, t in enumerate(times):
        while times[j] < t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


def _repeats(rows: list[dict], agents: dict) -> list[dict]:
    by: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["who"] and r["status"] not in ("failed", "repeat"):
            by.setdefault((r["audience"], r["who"], r["template"]), []).append(r)
    out = []
    for (aud, who, t), rs in by.items():
        times = sorted(r["at"] for r in rs)
        day, week = _peak(times, timedelta(hours=24)), _peak(times, timedelta(days=7))
        if day < REPEAT_DAY and week < REPEAT_WEEK:
            continue
        u = agents.get(rs[0]["owner"])
        name = (f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email) if u else "An agent"
        out.append({"template": t, "label": _label(t), "audience": aud, "agent_id": rs[0]["owner"],
                    "who": name if aud == "agent" else f"A client of {name}",
                    "in_a_day": day, "in_a_week": week, "total": len(rs),
                    "opened": sum(1 for r in rs if r["tracked"] and r["opened"]), "tracked": sum(1 for r in rs if r["tracked"])})
    out.sort(key=lambda x: (-x["in_a_day"], -x["in_a_week"], -x["total"]))
    return out


def _templates(rows: list[dict], repeats: list[dict]) -> list[dict]:
    by: dict[tuple, list[dict]] = {}
    for r in rows:
        by.setdefault((r["template"], r["audience"]), []).append(r)
    rep: dict[tuple, int] = {}
    for x in repeats:
        rep[(x["template"], x["audience"])] = rep.get((x["template"], x["audience"]), 0) + 1
    out = []
    for (t, aud), rs in by.items():
        people = {r["who"] for r in rs if r["who"]}
        tl = _tally(rs)
        out.append({"template": t, "label": _label(t), "audience": aud, **tl, "people": len(people),
                    "per_person": round(tl["sent"] / len(people), 1) if people else None,
                    "repetitive_people": rep.get((t, aud), 0),
                    "can_wait": aud == "agent" and not health.is_critical(t)})
    out.sort(key=lambda x: -(x["sent"] + x["failed"] + x["repeats_held"]))
    return out


async def _health_rows(db: AsyncSession) -> dict[str, dict]:
    """Every agent's state now: stored mode + how many unopened in a row (last 90 days)."""
    proven = await health.proven_templates(db)
    states = {h.user_id: h for h in (await db.execute(select(m.AgentEmailHealth))).scalars().all()}
    G = m.EmailLog
    rows = (await db.execute(select(G.owner_id, G.created_at, G.opened_at, G.clicked_at, G.template, G.tracked).where(
        G.audience == "agent", G.status != "failed", G.owner_id.isnot(None), G.created_at >= datetime.now(timezone.utc) - health.LOOKBACK))).all()
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r[0], []).append({"sent_at": r[1], "opened_at": r[2], "clicked_at": r[3], "template": r[4], "tracked": r[5]})
    out = {}
    for uid in set(by) | set(states):
        h = states.get(uid)
        d = health.decide(by.get(uid, []), mode=h.mode if h else "normal", warned_at=h.warned_at if h else None,
                          weekly_at=h.weekly_at if h else None, reset_at=h.reset_at if h else None, proven=proven)
        out[uid] = {"mode": h.mode if h else "normal", "unopened_in_a_row": d["unopened"], "held_now": len(h.held or []) if h else 0,
                    "held_total": h.held_total if h else 0, "since": h.changed_at if h and h.mode != "normal" else None}
    return out


async def metrics(db: AsyncSession, start: Optional[str], end: Optional[str]) -> dict:
    s, e = parse_range(start, end)
    lo, hi = _bounds(s, e)
    agents = await _agent_map(db)
    rows = await _emails(db, lo, hi)
    tz = _eastern()

    audiences = {a: _tally([r for r in rows if r["audience"] == a]) for a in ("agent", "client", "account")}
    days = []
    d = s
    while d <= e:
        rs = [r for r in rows if r["at"].astimezone(tz).date() == d]
        a, c = _tally([r for r in rs if r["audience"] == "agent"]), _tally([r for r in rs if r["audience"] == "client"])
        days.append({"d": d.isoformat(), "total": len([r for r in rs if r["status"] not in ("failed", "repeat")]),
                     "agent": a["sent"], "agent_opened": a["opened"], "client": c["sent"], "client_opened": c["opened"],
                     "account": sum(1 for r in rs if r["audience"] == "account" and r["status"] not in ("failed", "repeat"))})
        d += timedelta(days=1)

    repeats = _repeats(rows, agents)
    templates = _templates(rows, repeats)
    unopened = sorted([t for t in templates if t["tracked"] >= 3], key=lambda t: (t["open_rate"] or 0, -t["unopened"]))[:8]

    hstate = await _health_rows(db)
    per: dict[str, dict[str, list]] = {}
    for r in rows:
        if r["owner"]:
            per.setdefault(r["owner"], {"agent": [], "client": []})
            if r["audience"] in ("agent", "client"):
                per[r["owner"]][r["audience"]].append(r)
    table = []
    for uid, u in agents.items():
        p = per.get(uid, {"agent": [], "client": []})
        a, c = _tally(p["agent"]), _tally(p["client"])
        hs = hstate.get(uid, {"mode": "normal", "unopened_in_a_row": 0, "held_now": 0, "held_total": 0, "since": None})
        table.append({**_agent_row(u), "to_agent": a["sent"], "agent_tracked": a["tracked"], "agent_opened": a["opened"],
                      "agent_open_rate": a["open_rate"], "client_sent": c["sent"], "client_opened": c["opened"],
                      "client_open_rate": c["open_rate"], "bounced": a["bounced"] + c["bounced"] + a["spam"] + c["spam"],
                      "repetitive": sum(1 for x in repeats if x["agent_id"] == uid), **hs})
    table.sort(key=lambda r: ({"weekly": 0, "warned": 1}.get(r["mode"], 2), -(r["to_agent"] + r["client_sent"]), r["name"]))

    proven = await health.proven_templates(db, fresh=True)
    seen = sorted({r["template"] for r in rows if r["audience"] == "agent"})
    return {
        "start": s.isoformat(), "end": e.isoformat(), "days_count": (e - s).days + 1,
        "kpis": {"total": sum(1 for r in rows if r["status"] not in ("failed", "repeat")), "failed": sum(1 for r in rows if r["status"] == "failed"), "repeats_held": sum(1 for r in rows if r["status"] == "repeat"),
                 "opened": audiences["agent"]["opened"] + audiences["client"]["opened"],
                 "tracked": audiences["agent"]["tracked"] + audiences["client"]["tracked"],
                 "open_rate": pct(audiences["agent"]["opened"] + audiences["client"]["opened"], audiences["agent"]["tracked"] + audiences["client"]["tracked"]),
                 "bounced": sum(a["bounced"] for a in audiences.values()), "spam": sum(a["spam"] for a in audiences.values()),
                 "repetitive": len(repeats)},
        "audiences": audiences, "days": days, "templates": templates, "top_unopened": unopened,
        "repeats": repeats[:40], "agents": table,
        "health": {"warned": sum(1 for h in hstate.values() if h["mode"] == "warned"),
                   "weekly": sum(1 for h in hstate.values() if h["mode"] == "weekly"),
                   "held_now": sum(h["held_now"] for h in hstate.values()), "held_total": sum(h["held_total"] for h in hstate.values()),
                   "first_strike": health.FIRST_STRIKE, "second_strike": health.SECOND_STRIKE},
        "pixel": {"live": sorted(t for t in seen if t in proven), "waiting": sorted(t for t in seen if t not in proven),
                  "labels": {t: _label(t) for t in seen}},
        "rules": {"repeat_day": REPEAT_DAY, "repeat_week": REPEAT_WEEK},
    }


async def agent_detail(db: AsyncSession, agent_id: str, start: Optional[str], end: Optional[str]) -> Optional[dict]:
    u = await db.get(m.User, agent_id)
    if u is None:
        return None
    s, e = parse_range(start, end)
    lo, hi = _bounds(s, e)
    rows = await _emails(db, lo, hi, owner_id=agent_id)
    agents = {agent_id: u}
    repeats = _repeats(rows, agents)
    hs = (await _health_rows(db)).get(agent_id, {"mode": "normal", "unopened_in_a_row": 0, "held_now": 0, "held_total": 0, "since": None})
    G = m.EmailLog
    recent = (await db.execute(select(G.template, G.created_at, G.opened_at, G.clicked_at, G.tracked, G.status, G.delivery).where(
        G.owner_id == agent_id, G.audience == "agent").order_by(G.created_at.desc()).limit(25))).all()
    return {
        **_agent_row(u), "start": s.isoformat(), "end": e.isoformat(), **hs,
        "agent": _tally([r for r in rows if r["audience"] == "agent"]), "client": _tally([r for r in rows if r["audience"] == "client"]),
        "templates": _templates(rows, repeats), "repeats": repeats,
        "recent": [{"template": r[0], "label": _label(r[0]), "at": r[1], "opened_at": r[2], "clicked_at": r[3], "tracked": r[4],
                    "status": r[5], "delivery": r[6], "critical": health.is_critical(r[0])} for r in recent],
    }
