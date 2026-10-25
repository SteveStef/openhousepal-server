"""Invites' designed emails, rendered in code in the *_v2 look (the prod Mailgun domain is at its
template cap, so these never need a Mailgun template).

- invite_rsvp_agent      the agent's "New RSVP" alert (user, 10/24: it used to be one paragraph of
                          answers inside the generic New-lead email) - who, coming or not, party,
                          which day(s), lead / agent / has-an-agent, their answers as a tidy list.
- invite_day_before_agent the agent's "tomorrow" email the day before each day of an invite - who's
                          coming, how many, maybes, and anything they asked for.
- event_rsvp (render_guest_confirmation) - the guest's "You're on the list" email. Still sent as a
                          personal note from the agent (personal_email_service routes it here for its
                          body), so it goes from the agent's own mailbox when they connected one.

Every email is dark-mode safe the same way group 11 patched the v2 templates (color-scheme meta +
prefers-color-scheme rules + Outlook's [data-ogsc]/[data-ogsb]) - the ohp-* classes below."""
from __future__ import annotations

import html as _html
from typing import Any, Iterable, Optional

CODE_TEMPLATES = {"invite_rsvp_agent", "invite_day_before_agent"}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"
INK, TEXT, MUTED, GOLD = "#17140f", "#4a4f57", "#80868f", "#9a7a35"

_DARK_STYLE = """<style>
:root{color-scheme:light dark;supported-color-schemes:light dark}
body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}
table,td{mso-table-lspace:0pt;mso-table-rspace:0pt}
img{-ms-interpolation-mode:bicubic;border:0;outline:none;text-decoration:none;display:block}
table{border-collapse:collapse}
body{margin:0!important;padding:0!important;width:100%!important;background:#f2ede4}
a{color:#9a7a35}
@media screen and (max-width:620px){
  .container{width:100%!important}
  .pad{padding-left:22px!important;padding-right:22px!important}
  .h1{font-size:24px!important}
  .btn a{padding:16px 30px!important}
  .hide-sm{display:none!important}
}
@media (prefers-color-scheme:dark){
 body,.ohp-bg{background:#0f0e0c!important}
 .ohp-card{background:#1c1a16!important}
 .ohp-card2{background:#24211b!important;border-color:#3a3328!important}
 .ohp-ink{color:#f4efe6!important}
 .ohp-muted{color:#b8b0a2!important}
 .ohp-text{color:#ddd6c9!important}
 .ohp-green{color:#86d6a0!important}
 .ohp-greenbg{background:#15301f!important}
 .ohp-amberbg{background:#33280f!important}
 .ohp-amber{color:#f3cf7a!important}
 .ohp-gold,.ohp-gold a{color:#e6c47c!important}
 .btn td,.btn a{background:#e6c47c!important;border-color:#e6c47c!important;color:#17140f!important}
 .btn a *{color:#17140f!important}
}
[data-ogsb] .btn td,[data-ogsb] .btn a{background:#e6c47c!important;border-color:#e6c47c!important}
[data-ogsc] .btn a,[data-ogsc] .btn a *{color:#17140f!important}
[data-ogsb] .ohp-bg,[data-ogsb] body{background:#0f0e0c!important}
[data-ogsb] .ohp-card{background:#1c1a16!important}
[data-ogsb] .ohp-card2{background:#24211b!important;border-color:#3a3328!important}
[data-ogsc] .ohp-ink{color:#f4efe6!important}
[data-ogsc] .ohp-muted{color:#b8b0a2!important}
[data-ogsc] .ohp-text{color:#ddd6c9!important}
[data-ogsc] .ohp-green{color:#86d6a0!important}
[data-ogsb] .ohp-greenbg{background:#15301f!important}
[data-ogsb] .ohp-amberbg{background:#33280f!important}
[data-ogsc] .ohp-amber{color:#f3cf7a!important}
[data-ogsc] .ohp-gold{color:#e6c47c!important}
</style>"""


def e(v: Any) -> str:
    return _html.escape(str(v if v is not None else ""), quote=True)


def _tel(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"+1{digits[-10:]}" if len(digits) >= 10 else digits


# ------------------------------------------------------------------ building blocks

def eyebrow(text: str) -> str:
    return (f'<div style="font-size:11.5px;font-weight:800;letter-spacing:1.4px;text-transform:uppercase;color:{GOLD};'
            f'margin:0 0 8px 0;" class="ohp-gold">{e(text)}</div>')


def h1(text: str) -> str:
    return (f'<h1 class="h1 ohp-ink" style="margin:0;font-family:{SERIF};font-size:28px;line-height:1.2;font-weight:700;'
            f'color:{INK};">{e(text)}</h1>')


def para(html: str, *, top: int = 14, muted: bool = False, center: bool = False, size: float = 15.5) -> str:
    color, cls = (MUTED, "ohp-muted") if muted else (TEXT, "ohp-text")
    align = "text-align:center;" if center else ""
    return f'<p style="{align}margin:{top}px 0 0 0;font-size:{size}px;line-height:1.65;color:{color};" class="{cls}">{html}</p>'


def strong(text: str) -> str:
    return f'<strong style="color:{INK};font-weight:700;" class="ohp-ink">{e(text)}</strong>'


def card(inner: str, *, top: int = 18) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:{top}px;'
            f'border-collapse:separate;"><tr><td bgcolor="#faf7f1" style="background:#faf7f1;border:1px solid #ede4d3;'
            f'border-radius:14px;padding:16px 18px;" class="ohp-card2">{inner}</td></tr></table>')


def field(label: str, value_html: str, *, first: bool = False) -> str:
    return (f'<div style="margin-top:{0 if first else 12}px;"><div style="font-size:11px;font-weight:800;letter-spacing:1.2px;'
            f'text-transform:uppercase;color:{GOLD};margin:0 0 4px 0;" class="ohp-gold">{e(label)}</div>'
            f'<div style="font-size:15.5px;line-height:1.5;color:{INK};font-weight:600;" class="ohp-ink">{value_html}</div></div>')


def pill(text: str, tone: str = "gold") -> str:
    bg, color, cls = {
        "green": ("#e5f3e8", "#24663a", "ohp-greenbg ohp-green"),
        "amber": ("#fbf0d6", "#7a5410", "ohp-amberbg ohp-amber"),
        "gray": ("#f0efed", "#5b6068", "ohp-card2 ohp-muted"),
    }.get(tone, ("#f5eedf", "#6f5a2e", "ohp-card2 ohp-gold"))
    return (f'<span style="display:inline-block;background:{bg};color:{color};border-radius:999px;padding:4px 11px;'
            f'font-size:12px;font-weight:800;letter-spacing:.3px;margin:0 6px 6px 0;" class="{cls}">{e(text)}</span>')


def button(label: str, href: str, *, top: int = 28) -> str:
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:{top}px;">'
            f'<tr><td align="center"><table role="presentation" cellpadding="0" cellspacing="0" border="0" class="btn"><tr>'
            f'<td bgcolor="#17140f" align="center" style="background:#17140f;border-radius:12px;"><a href="{e(href)}" target="_blank" '
            f'style="display:inline-block;min-width:170px;padding:15px 30px;font-family:{FONT};font-size:15px;font-weight:800;'
            f'line-height:1.2;color:#ffffff;text-decoration:none;border-radius:12px;text-align:center;">{e(label)}</a></td></tr></table>'
            f'</td></tr></table>')


def a_link(label: str, href: str) -> str:
    return f'<a href="{e(href)}" style="color:{GOLD};font-weight:700;text-decoration:underline;" class="ohp-gold">{e(label)}</a>'


def contact_lines(email: Optional[str], phone: Optional[str]) -> str:
    out = []
    if email:
        out.append(f'<a href="mailto:{e(email)}" style="color:{GOLD};font-weight:700;text-decoration:none;" class="ohp-gold">{e(email)}</a>')
    if phone:
        out.append(f'<a href="tel:{e(_tel(phone))}" style="color:{GOLD};font-weight:700;text-decoration:none;" class="ohp-gold">{e(phone)}</a>')
    return "<br>".join(out)


def ohp_header(tag: str) -> str:
    return (f'<td valign="middle"><img src="https://openhousepal.com/email/ohp-logo-white.png" width="172" height="25" alt="OpenHousePal" '
            f'style="width:172px;height:25px;"></td><td valign="middle" align="right" style="font-family:{FONT};font-size:11px;font-weight:800;'
            f'letter-spacing:1.6px;text-transform:uppercase;color:#d9b460;white-space:nowrap;">{e(tag)}</td>')


def agent_header(v: dict, tag: str) -> str:
    photo = v.get("agent_photo_dark")
    img = (f'<img src="{e(photo)}" width="46" height="46" alt="{e(v.get("agent_name"))}" style="width:46px;height:46px;border-radius:23px;display:block;">'
           if photo else "")
    brokerage = (f'<div style="margin-top:3px;font-size:12px;line-height:1.3;font-weight:600;color:#d9b460;">{e(v.get("agent_brokerage"))}</div>'
                 if v.get("agent_brokerage") else "")
    return (f'<td valign="middle" width="1" style="padding:0;">{img}</td>'
            f'<td valign="middle" style="padding:0 0 0 {13 if photo else 0}px;font-family:{FONT};"><div style="font-family:{SERIF};font-size:18px;'
            f'line-height:1.2;font-weight:700;color:#ffffff;">{e(v.get("agent_name"))}</div>{brokerage}</td>'
            f'<td class="hide-sm" valign="middle" align="right" style="padding:0 0 0 12px;font-family:{FONT};font-size:11px;font-weight:800;'
            f'letter-spacing:1.4px;text-transform:uppercase;color:#d9b460;white-space:nowrap;">{e(tag)}</td>')


def agent_card(v: dict) -> str:
    """The agent's contact block at the bottom of a guest email (the transparent PNG avatar)."""
    if not v.get("agent_name"):
        return ""
    photo = (f'<td valign="top" width="70" style="width:70px;padding:0 14px 0 0;"><img src="{e(v["agent_photo"])}" width="56" height="56" '
             f'alt="{e(v.get("agent_name"))}" style="width:56px;height:56px;border-radius:28px;display:block;"></td>') if v.get("agent_photo") else ""
    lines = []
    for k in ("agent_title", "agent_brokerage"):
        if v.get(k):
            lines.append(f'<div style="margin-top:2px;font-size:12.5px;color:{MUTED};line-height:1.45;" class="ohp-muted">{e(v[k])}</div>')
    reach = contact_lines(v.get("agent_email"), v.get("agent_phone"))
    inner = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{photo}<td valign="top">'
             f'<div style="font-family:{SERIF};font-size:18px;font-weight:700;color:{INK};line-height:1.25;" class="ohp-ink">{e(v["agent_name"])}</div>'
             f'{"".join(lines)}<div style="margin-top:8px;font-size:14px;line-height:1.6;">{reach}</div></td></tr></table>')
    return card(inner, top=28)


def shell(*, title: str, preheader: str, header_cells: str, body: str, footer: str) -> str:
    pad = "&#8199;&#847;" * 12
    return (
        '<!doctype html><html lang="en" xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f'<title>{e(title)}</title>{_DARK_STYLE}</head>'
        '<body style="margin:0;padding:0;background:#f2ede4;" class="ohp-bg">'
        f'<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{e(preheader)}{pad}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f2ede4" style="background:#f2ede4;" class="ohp-bg">'
        '<tr><td align="center" style="padding:24px 12px 32px 12px;">'
        '<table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;">'
        '<tr><td bgcolor="#17140f" style="background:#17140f;border-radius:18px 18px 0 0;padding:18px 32px;" class="pad">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{header_cells}</tr></table></td></tr>'
        '<tr><td bgcolor="#c9a24d" style="background:#c9a24d;height:3px;line-height:3px;font-size:3px;">&nbsp;</td></tr>'
        '<tr><td bgcolor="#ffffff" class="pad ohp-card ohp-text" style="background:#ffffff;border-radius:0 0 18px 18px;padding:34px 36px 36px 36px;'
        f'font-family:{FONT};font-size:15.5px;line-height:1.6;color:{TEXT};">{body}</td></tr>'
        f'<tr><td class="pad ohp-muted" style="padding:22px 36px 0 36px;font-family:{FONT};font-size:12px;line-height:1.65;color:{MUTED};text-align:center;">{footer}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def _settings_footer(app_url: str) -> str:
    return (f'You get this email because it&rsquo;s switched on in <a href="{e(app_url)}/settings#notifications" '
            f'style="color:{MUTED};text-decoration:underline;" class="ohp-muted">Settings &rsaquo; Notifications</a>.')


def _answers_html(answers: Iterable[dict]) -> str:
    rows = []
    for i, a in enumerate(answers or []):
        val = a.get("value")
        text = ", ".join(str(x) for x in val) if isinstance(val, list) else str(val if val is not None else "")
        if not text.strip():
            continue
        rows.append(field(str(a.get("label") or "Answer"), e(text), first=not rows))
    return card("".join(rows)) if rows else ""


KIND_PILL = {
    "lead": ("New lead - no agent", "green"),
    "represented": ("Has an agent - not a lead", "gray"),
    "agent": ("Real estate agent", "gray"),
    "declined": ("Can't make it", "gray"),
}


# ------------------------------------------------------------------ the agent's RSVP alert

def render_rsvp_agent(subject: str, v: dict) -> tuple[str, str, str]:
    name = v.get("guest_name") or "A guest"
    response = v.get("rsvp_response") or "yes"
    word = {"yes": "is coming", "maybe": "might come", "no": "can't make it"}.get(response, "answered")
    title = f"{name} {word}"
    tag = "Updated RSVP" if v.get("changed") else "New RSVP"
    pills = []
    pills.append(pill({"yes": "Coming", "maybe": "Maybe", "no": "Can't make it"}.get(response, "RSVP"),
                      "green" if response == "yes" else "amber" if response == "maybe" else "gray"))
    if response != "no":
        n = int(v.get("guest_count") or 1)
        pills.append(pill("Just them" if n <= 1 else f"Party of {n}"))
    kind = v.get("lead_kind")
    if response == "no" and v.get("still_interested"):
        pills.append(pill("Still interested in the home", "green"))
    if v.get("keep_updated"):
        pills.append(pill("Wants updates on the home", "green"))
    if kind in KIND_PILL and not (kind == "declined"):
        label, tone = KIND_PILL[kind]
        pills.append(pill(label, tone))
    sub = (v.get("invite_subtitle") or "").strip()  # the flyer's own line 2 (round 8)
    intro = (f'For {strong(v.get("invite_title") or "your invite")}' + (f' &middot; {e(sub)}' if sub else "")
             + (f' &middot; {e(v["invite_when"])}' if v.get("invite_when") else "") + ".")
    details = []
    if v.get("days_label"):
        details.append(field("Coming on", e(v["days_label"]), first=True))
    reach = contact_lines(v.get("guest_email"), v.get("guest_phone"))
    if reach:
        details.append(field("Contact", reach, first=not details))
    body = [eyebrow(f'{tag} · {v.get("invite_kind") or "Invite"}'), h1(title), para(intro),
            f'<div style="margin-top:14px;">{"".join(pills)}</div>']
    if details:
        body.append(card("".join(details)))
    answers = _answers_html(v.get("answers") or [])
    if answers:
        body.append(f'<div style="margin-top:22px;font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:{MUTED};" class="ohp-muted">Their answers</div>')
        body.append(answers.replace('margin-top:18px', 'margin-top:8px', 1))
    totals = v.get("totals") or {}
    if totals:
        body.append(para(f'So far: {strong(str(totals.get("coming", 0)) + " coming")}'
                         + (f', {e(totals.get("maybe"))} maybe' if totals.get("maybe") else "")
                         + (f', {e(totals.get("no"))} can&rsquo;t make it' if totals.get("no") else "") + ".", top=18))
    body.append(button("See the guest list", v.get("link") or ""))
    if v.get("lead_link"):
        body.append(para(a_link("Open them in Leads", v["lead_link"]), center=True, top=14, size=14))
    note = {
        "lead": "They're not working with an agent - a follow-up is on your list.",
        "represented": "They're working with another agent, so they're not added as a new lead - no follow-up.",
        "agent": "Agents never become leads - no follow-up.",
    }.get(kind or "")
    if note:
        body.append(para(e(note), muted=True, center=True, top=12, size=13))
    html = shell(title=title, preheader=f"{title} - {v.get('invite_title') or ''}", header_cells=ohp_header(tag),
                 body="".join(body), footer=_settings_footer(v.get("app_url") or ""))
    text = [f"{tag}: {title}", f"For {v.get('invite_title') or 'your invite'}" + (f" - {v['invite_when']}" if v.get("invite_when") else "")]
    if response != "no":
        text.append(f"Party: {v.get('guest_count') or 1}")
    if v.get("days_label"):
        text.append(f"Coming on: {v['days_label']}")
    for k in ("guest_email", "guest_phone"):
        if v.get(k):
            text.append(str(v[k]))
    for a in v.get("answers") or []:
        val = a.get("value")
        text.append(f"{a.get('label')}: {', '.join(map(str, val)) if isinstance(val, list) else val}")
    text.append(f"See the guest list: {v.get('link') or ''}")
    return subject, html, "\n".join(text)


# ------------------------------------------------------------------ the agent's "tomorrow" email

def render_day_before(subject: str, v: dict) -> tuple[str, str, str]:
    coming = v.get("coming") or []
    maybe = v.get("maybe") or []
    title = f"Tomorrow: {v.get('invite_title') or 'your event'}"
    total = int(v.get("guests_coming") or 0)
    lead = (f'{strong(str(total) + (" person" if total == 1 else " people"))} coming' if total else "No yes RSVPs yet")
    lead += (f', {e(len(maybe))} maybe' if maybe else "") + f' &middot; {e(v.get("day_label") or "")}'
    body = [eyebrow("Tomorrow"), h1(v.get("invite_title") or "Your event"), para(lead)]
    if v.get("place"):
        body.append(card(field("Where", e(v["place"]), first=True)))

    def people(rows: list[dict], label: str) -> str:
        if not rows:
            return ""
        lines = []
        for r in rows:
            bits = [f'<span style="color:{INK};font-weight:700;" class="ohp-ink">{e(r.get("name") or "Guest")}</span>']
            if (r.get("guest_count") or 1) > 1:
                bits.append(f'party of {e(r["guest_count"])}')
            if r.get("tag"):
                bits.append(e(r["tag"]))
            if r.get("asked"):
                bits.append(f'<span style="color:{GOLD};font-weight:700;" class="ohp-gold">{e(r["asked"])}</span>')
            lines.append(f'<div style="padding:7px 0;border-bottom:1px solid #ede4d3;font-size:14.5px;line-height:1.45;color:{TEXT};" class="ohp-text">'
                         + " &middot; ".join(bits) + "</div>")
        return (f'<div style="margin-top:22px;font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:{MUTED};" class="ohp-muted">'
                f'{e(label)} ({len(rows)})</div>' + card("".join(lines), top=8))

    body.append(people(coming, "Coming"))
    body.append(people(maybe, "Maybe"))
    if not coming and not maybe:
        body.append(para("No one has said yes yet - a quick reminder today can still fill the room.", muted=True, top=16))
    body.append(button("Open the guest list", v.get("link") or ""))
    body.append(para("Tip: print the guest list or keep this email handy to greet people by name.", muted=True, center=True, top=12, size=13))
    html = shell(title=title, preheader=f"{total} coming tomorrow - {v.get('invite_title') or ''}",
                 header_cells=ohp_header("Tomorrow"), body="".join(body), footer=_settings_footer(v.get("app_url") or ""))
    text = [title, f"{total} coming, {len(maybe)} maybe - {v.get('day_label') or ''}"]
    text += [f"Coming: {r.get('name')}" + (f" (party of {r['guest_count']})" if (r.get('guest_count') or 1) > 1 else "") for r in coming]
    text += [f"Maybe: {r.get('name')}" for r in maybe]
    text.append(f"Guest list: {v.get('link') or ''}")
    return subject, html, "\n".join(text)


def render(template: str, subject: str, v: dict) -> tuple[str, str, str]:
    if template == "invite_day_before_agent":
        return render_day_before(subject, v)
    return render_rsvp_agent(subject, v)


# ------------------------------------------------------------------ the guest's confirmation

def render_guest_confirmation(subject: str, v: dict, link: str) -> tuple[str, str, str]:
    """"You're on the list" - wording fixed (user, 10/24: "You're on the list for 3 for Open House
    at 239 WINDHAM DR" read oddly; the raw link is now behind the words "update your RSVP")."""
    first = (v.get("first_name") or "").strip() or "there"
    resp = v.get("rsvp_response")
    what = (v.get("event_phrase") or v.get("invite_title") or "my event").strip()
    try:
        count = int(v.get("guest_count") or 1)
    except (TypeError, ValueError):
        count = 1
    agent = (v.get("agent_name") or "").strip()
    update = link or v.get("invite_link") or ""
    if resp == "no":
        head = "Thanks for letting me know"
        tag = "RSVP received"
        lead = f"Hi {e(first)}, thanks for letting me know you can&rsquo;t make it to {e(what)}. You&rsquo;ll be missed!"
        if v.get("still_interested"):
            lead += " I&rsquo;ll keep you posted on the home" + (" - you&rsquo;ll hear about price changes and new open houses." if v.get("keep_updated") else ".")
        tail = f'If your plans change, you can {a_link("update your RSVP", update)} any time.'
    else:
        head = f"You're on the list, {first}" if resp == "yes" else "Got it - you're a maybe"
        tag = "You're on the list" if resp == "yes" else "RSVP received"
        party = "" if count <= 1 else f" - party of {count} -"
        lead = (f"Hi {e(first)}, you&rsquo;re on the list{e(party)} for {e(what)}." if resp == "yes"
                else f"Hi {e(first)}, thanks - I&rsquo;ve got you down as a maybe for {e(what)}.")
        tail = f'The calendar invite is attached. If anything changes, you can {a_link("update your RSVP", update)} any time.'
    details = []
    if v.get("days_label") or v.get("invite_when"):
        details.append(field("When", e(v.get("days_label") or v.get("invite_when")), first=True))
    if v.get("invite_place"):
        details.append(field("Where", e(v["invite_place"]), first=not details))
    if resp != "no" and count > 1:
        details.append(field("Party", e(f"{count} people"), first=not details))
    body = [eyebrow(tag), h1(head), para(lead)]
    if details:
        body.append(card("".join(details)))
    body.append(para(tail, top=18))
    body.append(button("View the invitation", update))
    phone = (v.get("agent_phone") or "").strip()
    body.append(para(e(f"Questions? Just reply to this email{' or text me at ' + phone if phone else ''}."), muted=True, center=True, top=14, size=13))
    body.append(agent_card(v))
    footer = f"Sent by {e(agent or 'your host')} through OpenHousePal."
    html = shell(title=head, preheader=f"{head} - {what}", header_cells=agent_header(v, tag) if agent else ohp_header(tag),
                 body="".join(body), footer=footer)
    text_lead = _html.unescape(lead)
    text = [text_lead]
    if v.get("days_label") or v.get("invite_when"):
        text.append(f"When: {v.get('days_label') or v.get('invite_when')}")
    if v.get("invite_place"):
        text.append(f"Where: {v['invite_place']}")
    text.append(f"Update your RSVP: {update}")
    if agent:
        text.append(agent + (f"\n{phone}" if phone else ""))
    return subject, html, "\n\n".join(text)
