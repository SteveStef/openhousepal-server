"""Form insights - the Typeform-style "where do people drop off" analytics behind a form's Stats
tab. Pure functions over FormViewSession rows (one row per visit, see
forms_service.record_form_progress), so they're testable without a database.

Definitions (shown to agents in the UI, keep them in sync):
- View: someone opened the form (a visit).
- Started: answered/moved past the intro onto the first question.
- Shared contact: gave an email or phone (or finished) - the moment a visit becomes reachable.
- Completed: pressed Submit.
- Dropped off: started, didn't finish, and has been idle for ABANDON_AFTER. The day it happened
  is their last activity, Eastern time.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
ABANDON_AFTER = timedelta(minutes=30)

SOURCE_LABELS = {"qr": "QR code scan", "link": "Link", "website": "Your website"}
DEVICE_LABELS = {"mobile": "Phone", "tablet": "Tablet", "desktop": "Computer"}


def _day(dt: Optional[datetime]) -> Optional[date]:
    return dt.astimezone(EASTERN).date() if dt else None


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def question_order(questions: list[dict]) -> list[dict]:
    """The form's answerable questions (statements are screens, not questions)."""
    return [q for q in questions or [] if q.get("type") != "statement" and q.get("id")]


def session_path(s, questions: list[dict]) -> list[tuple[str, Optional[datetime]]]:
    """[(question_id, landed_at)] with consecutive repeats collapsed. Visits recorded before the
    path existed fall back to "every question up to the furthest step" (no timestamps)."""
    raw = getattr(s, "path", None) or []
    out: list[tuple[str, Optional[datetime]]] = []
    for e in raw:
        q = e.get("q") if isinstance(e, dict) else None
        if q and (not out or out[-1][0] != q):
            out.append((q, _parse(e.get("t"))))
    if out:
        return out
    qs = question_order(questions)
    n = min(getattr(s, "furthest_step_index", 0) or 0, len(qs))
    return [(q["id"], None) for q in qs[:n]]


def is_dropped(s, now: datetime) -> bool:
    if s.completed_at is not None or (s.furthest_step_index or 0) < 1:
        return False
    last = s.last_activity_at or s.updated_at or s.created_at
    return bool(last) and now - last >= ABANDON_AFTER


def has_contact(s) -> bool:
    return bool(s.completed_at or s.contact_email or s.contact_phone)


def build_insights(sessions: Iterable, questions: list[dict], *, days: int, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sessions = list(sessions)
    views = len(sessions)
    started = [s for s in sessions if (s.furthest_step_index or 0) >= 1]
    completed = [s for s in sessions if s.completed_at is not None]
    contact = [s for s in started if has_contact(s)]
    dropped = [s for s in sessions if is_dropped(s, now)]
    dropped_ids = {id(s) for s in dropped}

    def pct(n, d):
        return round(n / d * 100, 1) if d else 0.0

    # ---- time to complete (median, seconds) ----
    durations = []
    for s in completed:
        begin = s.started_at or s.created_at
        if begin and s.completed_at and s.completed_at > begin:
            durations.append((s.completed_at - begin).total_seconds())

    # ---- day by day (Eastern), every day in range filled ----
    end = _day(now)
    if days:
        start = end - timedelta(days=days - 1)
    else:
        firsts = [_day(s.created_at) for s in sessions if s.created_at]
        start = min(firsts) if firsts else end
        start = max(start, end - timedelta(days=365))
    buckets = {start + timedelta(days=i): Counter() for i in range((end - start).days + 1)}
    for s in sessions:
        for key, when in (("views", s.created_at), ("starts", s.started_at if (s.furthest_step_index or 0) >= 1 else None),
                          ("completions", s.completed_at)):
            d = _day(when)
            if d in buckets:
                buckets[d][key] += 1
    for s in dropped:
        d = _day(s.last_activity_at or s.updated_at or s.created_at)
        if d in buckets:
            buckets[d]["dropOffs"] += 1
    daily = [{"date": d.isoformat(), "views": c["views"], "starts": c["starts"],
              "completions": c["completions"], "dropOffs": c["dropOffs"]} for d, c in sorted(buckets.items())]

    # ---- per question: who saw it, who left there, how long they spent ----
    qs = question_order(questions)
    seen: Counter = Counter()
    left_here: Counter = Counter()
    time_on: dict[str, list[float]] = defaultdict(list)
    for s in sessions:
        path = session_path(s, questions)
        if not path:
            continue
        for qid in {q for q, _ in path}:
            seen[qid] += 1
        if id(s) in dropped_ids:
            left_here[path[-1][0]] += 1
        for i, (qid, t) in enumerate(path):
            nxt = path[i + 1][1] if i + 1 < len(path) else (s.completed_at if s.completed_at else None)
            if t and nxt and nxt > t:
                secs = (nxt - t).total_seconds()
                if secs <= 3600:  # someone who left the tab open overnight isn't "time spent"
                    time_on[qid].append(secs)
    per_question = []
    for i, q in enumerate(qs, start=1):
        qid = q["id"]
        n_seen = seen.get(qid, 0)
        n_left = left_here.get(qid, 0)
        per_question.append({
            "questionId": qid,
            "label": q.get("label") or qid,
            "number": i,
            "seenCount": n_seen,
            "droppedCount": n_left,
            "dropOffPercent": pct(n_left, n_seen),
            "medianSeconds": round(median(time_on[qid]), 1) if time_on.get(qid) else None,
            # kept for the older shape
            "reachedCount": n_seen,
            "reachedPercent": pct(n_seen, views),
        })
    leak = max((q for q in per_question if q["droppedCount"] >= 2), key=lambda q: (q["droppedCount"], q["dropOffPercent"]), default=None)

    def split(key: str, labels: dict) -> list[dict]:
        tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for s in sessions:
            k = getattr(s, key, None)
            if not k:
                continue  # every visit records its source + device; a blank one is test/old data
            tally[k][0] += 1
            tally[k][1] += 1 if s.completed_at else 0
        rows = [{"key": k, "label": labels.get(k, "Unknown"), "views": v, "completions": c,
                 "completionRate": pct(c, v)} for k, (v, c) in tally.items()]
        return sorted(rows, key=lambda r: -r["views"])

    referrers = Counter(s.referrer for s in sessions if s.referrer)

    # ---- when people drop off: weekday x part of day (Eastern) ----
    heat = [[0] * 4 for _ in range(7)]  # [weekday][night, morning, afternoon, evening]
    for s in dropped:
        last = (s.last_activity_at or s.updated_at or s.created_at).astimezone(EASTERN)
        heat[last.weekday()][0 if last.hour < 6 else 1 if last.hour < 12 else 2 if last.hour < 18 else 3] += 1

    return {
        "rangeDays": days,
        "startRate": pct(len(started), views),
        "contactCount": len(contact),
        "contactRate": pct(len(contact), views),
        "dropOffCount": len(dropped),
        "inProgressCount": sum(1 for s in started if s.completed_at is None and id(s) not in dropped_ids),
        "medianCompletionSeconds": round(median(durations), 1) if durations else None,
        "funnel": [
            {"key": "views", "label": "Opened the form", "count": views},
            {"key": "started", "label": "Started answering", "count": len(started)},
            {"key": "contact", "label": "Shared contact info", "count": len(contact)},
            {"key": "completed", "label": "Submitted", "count": len(completed)},
        ],
        "daily": daily,
        "questions": per_question,
        "biggestLeak": leak,
        "sources": split("source", SOURCE_LABELS),
        "devices": split("device", DEVICE_LABELS),
        "referrers": [{"host": h, "count": c} for h, c in referrers.most_common(5)],
        "dropOffHeat": heat,
    }
