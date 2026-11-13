"""The listing agent's email for an "I'd host any of these" ask (owner, 11/12): one line per requested
open - home, day, time - instead of the one-line Mailgun host_request_received template squeezing a
list into a paragraph. Built here in the same v2 look as the other code-rendered emails
(invite_email_service), since the prod Mailgun domain is at its template cap."""
from __future__ import annotations

from typing import Optional

from app.services.invite_email_service import (GOLD, INK, MUTED, SERIF, TEXT, _settings_footer, button, card, e, eyebrow, h1,
                                               ohp_header, para, pill, shell, strong)

CODE_TEMPLATES = {"host_request_group_received"}

# kind -> (badge, how-to line); the title is built from the numbers.
_KIND = {
    "one_day": ("Pick one", "Same day for all - pick the one home you want hosted."),
    "same_set": ("One per day", "Any of these homes on those days - pick one home for each day you want."),
    "each": ("Pick any", "A day and time for each home - say yes to as many as you like."),
}


def _row(o: dict, first: bool) -> str:
    img = o.get("image") or ""
    thumb = (f'<td valign="middle" width="76" style="width:76px;padding:0 14px 0 0;"><img src="{e(img)}" width="76" height="57" alt="" '
             f'style="width:76px;height:57px;border-radius:9px;object-fit:cover;display:block;"></td>') if img else ""
    border = "" if first else "border-top:1px solid #ede4d3;"
    return (f'<tr><td style="padding:12px 0;{border}"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{thumb}'
            f'<td valign="middle"><div style="font-size:15.5px;line-height:1.35;font-weight:800;color:{INK};" class="ohp-ink">{e(o.get("street"))}</div>'
            f'<div style="margin-top:3px;font-size:14px;line-height:1.4;font-weight:600;color:{GOLD};" class="ohp-gold">{e(o.get("when"))}</div>'
            f'</td></tr></table></td></tr>')


def render(template: str, subject: str, v: dict) -> tuple[str, str, str]:
    opens = v.get("opens") or []
    homes = int(v.get("homes") or len({o.get("street") for o in opens}) or 1)
    kind = v.get("kind") if v.get("kind") in _KIND else "each"
    badge, how = _KIND[kind]
    who = v.get("requester_name") or "An agent"
    first = (v.get("agent_name") or "").split(" ")[0] or "there"
    title = f"{who} would host {'one' if kind == 'one_day' else 'any'} of these {homes} homes"
    app = (v.get("app_url") or "").rstrip("/")
    link = f"{app}/open-houses/my-listings?panel=host-requests"
    intro = (f"Hi {e(first)}, {strong(who)}" + (f" ({e(v['requester_brokerage'])})" if v.get("requester_brokerage") else "")
             + " would like to host an open house for you.")
    rows = "".join(_row(o, i == 0) for i, o in enumerate(opens))
    body = [
        eyebrow("Your listings"), h1(title), para(intro),
        f'<div style="margin-top:14px;">{pill(badge)}</div>',
        para(e(how), top=10, size=14.5),
        card(f'<div style="font-size:11px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:{MUTED};" class="ohp-muted">'
             f'Requested opens ({len(opens)})</div>'
             f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:4px;">{rows}</table>'),
    ]
    if v.get("note"):
        body.append(para(f'<span style="font-style:italic;color:{TEXT};" class="ohp-text">&ldquo;{e(v["note"])}&rdquo;</span>', top=16))
    body.append(button("Pick one" if kind == "one_day" else "Review the request", link))
    body.append(para(e(f"{who.split(' ')[0]} can host one open a day, so a yes closes their others that same day. "
                       "Or answer them all with None of these."), muted=True, center=True, top=12, size=13))
    html = shell(title=title, preheader=f"{title} - {opens[0]['when'] if len(opens) == 1 else badge}", header_cells=ohp_header("Host request"),
                 body="".join(body), footer=_settings_footer(app))
    text = [title, "", f"Hi {first}, {who} would like to host an open house for you.", how, ""]
    text += [f"- {o.get('street')}: {o.get('when')}" for o in opens]
    if v.get("note"):
        text += ["", f'"{v["note"]}"']
    text += ["", f"Review it: {link}"]
    return subject, html, "\n".join(text)
