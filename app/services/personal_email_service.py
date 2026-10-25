"""The Showcase invite emails, written like a note the agent typed themselves.

Why (user, 10/17): the first email after an open house - from the agent's own Gmail, SPF/DKIM/DMARC
all passing - landed in Gmail's Promotions tab. Authentication was fine; the CONTENT read as
marketing: branded HTML, a photo, a big button, "26 picked for you", a 3-step card, hidden preheader
padding, a tracking pixel and a redirect link on another domain. Gmail sorts tabs mostly on what a
message looks like, so these two personal notes (the open house invite and a Showcase the agent
built for one client) are now plain: a few short paragraphs, the real link written out (the text
and the address match - no redirect), a typed-style signature, no images, no pixel, no preheader.

Unsubscribe stays: one quiet line at the bottom (CAN-SPAM wants a clear way out of a message that
promotes homes). From the agent's own mailbox the List-Unsubscribe HEADER is left off - that header
marks bulk mail, and one note from one person isn't. From OpenHousePal's shared domain it stays on
(the bulk-sender rules apply there and complaints there hurt every agent).

The recurring automatic updates (daily / weekly Showcase digests, Spotlight alerts) keep their
designed Mailgun templates, headers and tracking - that's fine for them."""
from __future__ import annotations

import html as _html
from typing import Any

PERSONAL_TEMPLATES = {"visitor_confirmation", "showcase_shared", "event_invite", "event_rsvp", "spotlight_documents"}
# spotlight_documents (10/24): the documents a guest asked for on a Listing Spotlight - a short note
# with each document's link written out (no Mailgun template; the prod domain is at its cap).
# Invites (invites_service): the invitation itself and the guest's RSVP confirmation - personal
# notes too, so they need no Mailgun template (the prod domain is at its template cap).
INVITE_TEMPLATES = {"event_invite", "event_rsvp"}

_WRAP = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;color:#222222;'
         'max-width:560px">{body}</div>')
_P = '<p style="margin:0 0 14px 0">{}</p>'


def _e(v: Any) -> str:
    return _html.escape(str(v or ""), quote=True)


def _street(addr: str) -> str:
    """"1330 N Avignon Dr, Gladwyne, PA 19035" -> "1330 N Avignon Dr"."""
    from app.services.agent_roundup_service import nice_street
    return nice_street((addr or "").split(",")[0])


def _signature(v: dict) -> tuple[list[str], list[str]]:
    """(html lines, text lines) - name, title · brokerage, phone, email."""
    name = (v.get("agent_name") or "").strip()
    role = " · ".join(x for x in ((v.get("agent_title") or "").strip(), (v.get("agent_brokerage") or "").strip()) if x)
    phone = (v.get("agent_phone") or "").strip()
    email = (v.get("agent_email") or "").strip()
    text = [x for x in (name, role, phone, email) if x]
    html = []
    if name:
        html.append(_e(name))
    if role:
        html.append(f'<span style="color:#555555">{_e(role)}</span>')
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        html.append(f'<a href="tel:+1{digits[-10:]}" style="color:#222222;text-decoration:none">{_e(phone)}</a>' if len(digits) >= 10 else _e(phone))
    if email:
        html.append(f'<a href="mailto:{_e(email)}" style="color:#222222;text-decoration:none">{_e(email)}</a>')
    return html, text


def render(template: str, subject: str, v: dict, *, link: str) -> tuple[str, str, str]:
    """(subject, html, text) for one of PERSONAL_TEMPLATES. `link` is the Showcase address to show
    (already carrying the tracking marker)."""
    first = (v.get("first_name") or v.get("recipient_name") or "").strip() or "there"
    phone = (v.get("agent_phone") or "").strip()
    reach = f"just reply to this email or text me at {phone}" if phone else "just reply to this email"
    if template == "event_rsvp":
        # The guest's RSVP confirmation is a designed v2-look email now (user, 10/24) - it's a reply
        # to something they just did, not an unexpected note, so the Promotions worry above doesn't
        # apply. Still sent as the agent's personal note (their mailbox / their name).
        from app.services.invite_email_service import render_guest_confirmation
        return render_guest_confirmation(subject, v, link or (v.get("invite_link") or ""))
    if template in INVITE_TEMPLATES:
        link = link or (v.get("invite_link") or "")
        title = (v.get("invite_title") or "").strip() or "my event"
        # the flyer's line 2 when the agent typed their own (round 8) - unless it just repeats the place
        sub = (v.get("invite_subtitle") or "").strip()
        _n = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())  # noqa: E731
        if sub and (_n(sub) in _n(v.get("invite_place") or "") or _n(sub) == _n(title)):
            sub = ""
        if sub:
            title = f"{title} ({sub})"
        when = (v.get("invite_when") or "").strip()
        place = (v.get("invite_place") or "").strip()
        details = " · ".join(x for x in (when, place) if x)
        if template == "event_invite":
            subject = (f"Reminder: {title}" if v.get("is_reminder") else title)[:200]
            note = (v.get("invite_message") or "").strip()
            paras = [f"Hi {first},",
                     ("Just a friendly reminder - I'd love to have you at " if v.get("is_reminder") else "I'd love for you to join me for ")
                     + f"{title}{(' - ' + details) if details else ''}."]
            if note:
                paras.append(note)
            paras += ["Here's your invitation - you can RSVP right on the page, it takes a few seconds:", "__LINK__",
                      f"If you have any questions, {reach}."]
        else:  # event_rsvp - the guest's confirmation
            resp = v.get("rsvp_response")
            if resp == "no":
                paras = [f"Hi {first},", f"Thanks for letting me know you can't make it to {title}. You'll be missed!",
                         "If your plans change, you can update your RSVP here:", "__LINK__"]
            else:
                try:
                    count = int(v.get("guest_count") or 1)
                except (TypeError, ValueError):
                    count = 1
                party = f" for {count}" if count > 1 else ""
                lead = f"You're on the list{party} for {title}." if resp == "yes" else f"Thanks - I've got you down as a maybe for {title}."
                paras = [f"Hi {first},", lead + (f" {details}." if details else ""),
                         "It's in the calendar invite attached. If anything changes, you can update your RSVP here:", "__LINK__",
                         f"See you there! If you have any questions, {reach}."]
    elif template == "spotlight_documents":
        street = _street(v.get("property_address") or "")
        docs = [d for d in (v.get("documents") or []) if isinstance(d, dict) and d.get("url")]
        one = len(docs) == 1
        what = f"the {docs[0].get('label') or 'document'}" if one else "the documents"
        paras = [f"Hi {first},", f"{'Here is' if one else 'Here are'} {what} for {street or 'the home'} you asked for:", "__DOCS__"]
        if v.get("site_url"):
            paras += ["Every photo and detail of the home is here:", "__SITE__"]
        paras.append(f"If you have any questions about it, {reach}.")
    elif template == "visitor_confirmation":
        street = _street(v.get("property_address") or v.get("test_property_address") or "")
        subject = f"Following up from the open house at {street}" if street else "Following up from the open house"
        try:
            count = int(v.get("properties_count") or 0)
        except (TypeError, ValueError):
            count = 0
        paras = [
            f"Hi {first},",
            f"Thanks for stopping by the open house{(' at ' + street) if street else ''}. It was great to meet you.",
            ("I put together a list of homes like it that you might want to look at. It keeps itself up to date, "
             "so new listings and price drops show up there as they happen:") if count else
            ("I set up a list for you that fills in with homes like it as they come on the market, "
             "along with any price drops:"),
            "__LINK__",
            f"If one catches your eye or you'd like to see it in person, {reach}.",
        ]
    else:  # showcase_shared - the agent built it by hand for one client
        try:
            total = int(v.get("total_count") or 0)
        except (TypeError, ValueError):
            total = 0
        summary = (v.get("search_summary") or "").strip().rstrip(".")
        subject = "Some homes I picked out for you" if total else "Your home search is set up"
        paras = [
            f"Hi {first},",
            ("I put together a list of homes that fit what you're looking for" + (f" ({summary})" if summary else "") + ". Here's the link:")
            if total else
            ("I set up your home search" + (f" ({summary})" if summary else "") + ". New homes will show up here as they hit the market:"),
            "__LINK__",
            "It updates on its own when new homes come on the market or a price drops. If you want to change what it looks for, "
            "you can do that right on the page, or just tell me.",
            f"If you'd like to see any of them, {reach}.",
        ]
    sig_html, sig_text = _signature(v)
    unsub = (v.get("Unsub") or "").strip()

    body = []
    text = []
    for p in paras:
        if p == "__DOCS__":
            for d in (v.get("documents") or []):
                if not isinstance(d, dict) or not d.get("url"):
                    continue
                label, url = d.get("label") or "Document", d["url"]
                body.append(_P.format(f'<b>{_e(label)}</b><br><a href="{_e(url)}" style="color:#1a56b8">{_e(url)}</a>'))
                text.append(f"{label}\n{url}")
            continue
        if p == "__SITE__":
            site = v.get("site_url") or ""
            body.append(_P.format(f'<a href="{_e(site)}" style="color:#1a56b8">{_e(site)}</a>'))
            text.append(site)
            continue
        if p == "__LINK__":
            body.append(_P.format(f'<a href="{_e(link)}" style="color:#1a56b8">{_e(link)}</a>'))
            text.append(link)
        else:
            body.append(_P.format(_e(p)))
            text.append(p)
    if sig_html:
        body.append('<p style="margin:18px 0 0 0">' + "<br>".join(sig_html) + "</p>")
        text.append("\n".join(sig_text))
    if unsub:
        body.append(f'<p style="margin:28px 0 0 0;font-size:12px;color:#888888">Rather not get these? '
                    f'<a href="{_e(unsub)}" style="color:#888888">Unsubscribe</a></p>')
        text.append(f"Rather not get these? Unsubscribe: {unsub}")
    return subject, _WRAP.format(body="".join(body)), "\n\n".join(text)


def with_marker(url: str, track_id: str) -> str:
    """The real Showcase address plus ?ce=<id> - the page reports the visit itself (POST /e/v/<id>),
    so the email's link text and address are the same and nothing redirects through another domain.
    Link scanners don't run JavaScript, so their fetches never count as a click."""
    if not url or not track_id:
        return url
    return f"{url}{'&' if '?' in url else '?'}ce={track_id}"
