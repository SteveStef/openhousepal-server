"""Lead warmth - one number for "how active is this person right now", built from what they've
already done (every signal is a LeadEvent: Showcase views/likes/comments, email opens/clicks,
forms, tour and second-visit requests, sign-ins...). Recent activity counts most, so a guest who
went quiet drops back down on their own. Also flags "heating up": someone whose activity this
week is a big jump on their usual (e.g. barely looking, then five Showcase views this week) - the
moment to reach out. No new tracking: it only reads the events the Leads list already loads.

Two halves, the higher one wins:
- Overall activity: points per action x how recent it is, x5, capped at 100.
- One-home interest: everything they did on a single home in the last 2 weeks. Coming back to
  the same house, liking it, asking about it, asking to see it - that's someone ready to move, so
  it lifts them straight to the top (up to 100), even if they were quiet before that match came in.

Fairness guard rails (the score only orders the list and adds a label - it never hides anyone,
changes their emails or skips a follow-up):
- Only things the guest did count. The agent's own Showcase previews never log; emails we sent,
  unsubscribes and pauses carry no points.
- Can't be pumped: a home's view counts once per 12 hours (logged that way), at most 5 view points
  a day overall, and at most 1 email-open point a day (Apple Mail "opens" emails on its own).
- Fades on its own after 30 days, so nobody stays "Hot" forever off one busy week.
- Brand-new leads are never "heating up" (a new sign-in is already on the agent's radar)."""
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

# How much each kind of activity says "this person is in the market now".
WEIGHTS: dict[str, float] = {
    'property_tour': 6, 'property_website_tour_request': 6, 'property_page_tour_request': 6,
    'open_house_second_visit': 6, 'open_house_disclosure': 5,
    'property_website_inquiry': 5, 'property_page_inquiry': 5, 'blog_inquiry': 4, 'portal_lead': 4,
    'form_submission': 4, 'open_house_visitor': 4, 'property_comment': 3,
    'showcase_like': 2, 'showcase_refine': 2, 'showcase_search_changed': 2, 'email_clicked': 2,
    'showcase_view': 1, 'collection_visitor': 1, 'showcase_dislike': 1, 'email_opened': 1, 'form_partial': 1,
    'review_left': 1,
}

# Per-day caps on the cheap signals (points per calendar day, UTC).
DAILY_CAP: dict[str, float] = {'showcase_view': 5, 'collection_visitor': 2, 'email_opened': 1, 'showcase_dislike': 3}

# "Just looking" signals. However many, they top out at Warm (BROWSE_CAP x5 = 30): scrolling lots of
# homes, passing on homes or opening emails isn't the same as wanting one. Hot takes a real signal
# (a like, comment, click, question, form, sign-in, showing ask) or interest in one home (below).
BROWSE_TYPES = {'showcase_view', 'collection_visitor', 'showcase_dislike', 'email_opened', 'form_partial',
                'showcase_refine', 'showcase_search_changed'}
BROWSE_CAP = 6
# Email opens alone stay Cool at most (Apple Mail opens emails by itself): 2 points = 10.
OPEN_CAP = 2

# Interest in ONE home: what each action on a specific house is worth. A pass ("not for me") is
# not interest; liking and asking count more than in the overall score because they're aimed.
HOME_WEIGHTS: dict[str, float] = {
    'showcase_view': 1, 'showcase_like': 3, 'property_comment': 3,
    'property_website_inquiry': 5, 'property_page_inquiry': 5, 'open_house_disclosure': 5,
    'property_tour': 6, 'property_website_tour_request': 6, 'property_page_tour_request': 6,
    'open_house_second_visit': 6,
}
HOME_WINDOW_DAYS = 14
# Home points -> score: 4 (e.g. back to the same house on 4 different days) = 64 Hot,
# a showing request alone = 76, a few looks + a like + a showing request = 100.
HOME_MIN_POINTS = 4

# Plain words for the "this week" line ("5 Showcase views this week").
_NOUN: dict[str, tuple[str, str]] = {
    'showcase_view': ('Showcase view', 'Showcase views'), 'collection_visitor': ('Showcase visit', 'Showcase visits'),
    'showcase_like': ('liked home', 'liked homes'), 'property_comment': ('comment', 'comments'),
    'email_opened': ('email opened', 'emails opened'), 'email_clicked': ('email click', 'email clicks'),
    'form_submission': ('form filled out', 'forms filled out'),
    'property_tour': ('showing request', 'showing requests'), 'property_website_tour_request': ('showing request', 'showing requests'),
    'property_page_tour_request': ('showing request', 'showing requests'), 'open_house_second_visit': ('second-visit request', 'second-visit requests'),
    'open_house_visitor': ('open house sign-in', 'open house sign-ins'), 'open_house_disclosure': ('disclosure request', 'disclosure requests'),
    'property_website_inquiry': ('listing question', 'listing questions'), 'property_page_inquiry': ('listing question', 'listing questions'),
    'blog_inquiry': ('blog question', 'blog questions'), 'portal_lead': ('portal inquiry', 'portal inquiries'),
    'showcase_refine': ('search change', 'search changes'), 'showcase_search_changed': ('search change', 'search changes'),
    'showcase_dislike': ('home passed on', 'homes passed on'), 'form_partial': ('form started', 'forms started'),
    'review_left': ('review', 'reviews'),
}
_ASKS = {'property_tour', 'property_website_tour_request', 'property_page_tour_request', 'open_house_second_visit'}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _recency(days: float) -> float:
    if days <= 7:
        return 1.0
    if days <= 14:
        return 0.6
    if days <= 30:
        return 0.3
    return 0.0


def _home_label(addr: str) -> str:
    street = (addr or '').split(',')[0].strip()
    if street.isupper():  # MLS shouting ("129 LOCUST GROVE RD") -> "129 Locust Grove Rd"
        street = ' '.join(w.capitalize() for w in street.split())
    return street or 'one home'


def _home_reason(addr: str, kinds: dict[str, int]) -> str:
    bits = []
    looks = kinds.get('showcase_view', 0)
    if looks:
        bits.append(f"{looks} look{'s' if looks != 1 else ''}")
    if kinds.get('showcase_like'):
        bits.append('liked it')
    comments = kinds.get('property_comment', 0)
    if comments:
        bits.append(f"{comments} comments" if comments > 1 else 'commented')
    if any(kinds.get(k) for k in ('property_website_inquiry', 'property_page_inquiry', 'open_house_disclosure')):
        bits.append('asked about it')
    if any(kinds.get(k) for k in _ASKS):
        bits.append('asked to see it')
    return f"Into {_home_label(addr)}" + (f" - {', '.join(bits)}" if bits else '')


def warmth(events: Iterable, created_at: Optional[datetime] = None, now: Optional[datetime] = None) -> dict:
    """{score 0-100, level hot|warm|cool|cold, heating_up, reason}."""
    now = now or datetime.now(timezone.utc)
    raw = 0.0      # real signals
    browse = 0.0   # just-looking signals (capped)
    opens = 0.0
    week: dict[str, int] = {}
    week_points = 0.0
    before = 0.0  # points 8-35 days ago - how active were they before this week?
    first: Optional[datetime] = None
    day_used: dict[tuple, float] = defaultdict(float)
    homes: dict[str, float] = defaultdict(float)
    home_kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    home_name: dict[str, str] = {}
    evs = [e for e in events if getattr(e, 'occurred_at', None) is not None]
    evs.sort(key=lambda e: _aware(e.occurred_at))
    for e in evs:
        st = getattr(e, 'source_type', '') or ''
        w = WEIGHTS.get(st)
        if not w:
            continue
        at = _aware(e.occurred_at)
        if at > now:
            continue
        first = at if first is None else first
        days = (now - at).total_seconds() / 86400
        # One home: everything aimed at the same house in the last 2 weeks.
        addr = (getattr(e, 'property_address', None) or '').strip().lower()
        hw = HOME_WEIGHTS.get(st)
        if addr and hw and days <= HOME_WINDOW_DAYS:
            homes[addr] += hw * (1.0 if days <= 7 else 0.6)
            home_kinds[addr][st] += 1
            home_name.setdefault(addr, e.property_address.strip())
        # Overall activity, with the cheap signals capped per day.
        cap = DAILY_CAP.get(st)
        if cap is not None:
            key = (st, at.date())
            w = min(w, cap - day_used[key])
            if w <= 0:
                continue
            day_used[key] += w
        if st == 'email_opened':
            opens += w * _recency(days)
        elif st in BROWSE_TYPES:
            browse += w * _recency(days)
        else:
            raw += w * _recency(days)
        if days <= 7:
            week_points += w
            week[st] = week.get(st, 0) + 1
        elif days <= 35:
            before += w
    score = min(100, round((raw + min(BROWSE_CAP, browse + min(OPEN_CAP, opens))) * 5))
    reason = None
    if week:
        # Same words = same thing ("search change" covers both search-edit events).
        by_noun: dict[tuple[str, str], list] = {}
        for st, n in week.items():
            k = _NOUN.get(st, ('new activity', 'new activities'))
            cur = by_noun.setdefault(k, [0, 0.0])
            cur[0] += n
            cur[1] += WEIGHTS.get(st, 0) * n
        (one, many), (n, _) = max(by_noun.items(), key=lambda kv: (kv[1][1], kv[1][0]))
        reason = f"{n} {one if n == 1 else many} this week"
    # One-home interest lifts them up to 100.
    if homes:
        addr, pts = max(homes.items(), key=lambda kv: kv[1])
        if pts >= HOME_MIN_POINTS:
            home_score = min(100, round(40 + pts * 6))
            if home_score >= score:
                score = home_score
                reason = _home_reason(home_name[addr], home_kinds[addr])
    level = 'hot' if score >= 40 else 'warm' if score >= 15 else 'cool' if score > 0 else 'cold'
    # Heating up: a real jump this week vs. their usual week over the 4 weeks before - quiet, or
    # just barely looking, then suddenly busy - and not a brand-new lead.
    known_for = (now - _aware(created_at)).days if created_at else ((now - first).days if first else 0)
    usual_week = before / 4
    heating = week_points >= 4 and week_points >= 3 * max(usual_week, 1) and known_for >= 10
    return {'score': score, 'level': level, 'heating_up': heating, 'reason': reason}
