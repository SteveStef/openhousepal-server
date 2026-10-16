"""Where tapping a notification takes the agent: the exact thing it talks about (that lead, that
form answer, that follow-up, that listing's host requests...), never just the page it lives on.

Worked out when notifications are READ, from reference_type/reference_id, so every older
notification (minted before a link was made exact, or with a link that has since moved) lands in
the right place too - the stored `link` is only the fallback. Used by the bell, the Activity page
and Dash, which all read GET /api/v1/notifications."""
from datetime import date, timedelta
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import FormSubmission, Lead, LeadEvent, Notification, OpenHouseHostRequest

_TOUR_SOURCE_TYPES = ("property_tour", "property_website_tour_request", "property_page_tour_request")
_INQUIRY_SOURCE_TYPES = ("property_page_inquiry", "property_website_inquiry")


def _ids(notifications: List[Notification], reference_type: str) -> List[str]:
    return list({n.reference_id for n in notifications if n.reference_type == reference_type and n.reference_id})


async def exact_links(db: AsyncSession, agent_id: str, notifications: List[Notification]) -> Dict[str, str]:
    """notification id -> the exact link (only for those whose target could be pinned down)."""
    out: Dict[str, str] = {}

    async def lead_by_source(source_types, source_ids) -> Dict[str, str]:
        if not source_ids:
            return {}
        rows = await db.execute(
            select(LeadEvent.source_id, LeadEvent.lead_id).join(Lead, Lead.id == LeadEvent.lead_id)
            .where(Lead.owner_id == agent_id, LeadEvent.source_type.in_(source_types), LeadEvent.source_id.in_(source_ids))
        )
        return {sid: lid for sid, lid in rows.all()}

    visitor_leads = await lead_by_source(("open_house_visitor",), _ids(notifications, "VISITOR"))
    tour_leads = await lead_by_source(_TOUR_SOURCE_TYPES, _ids(notifications, "TOUR"))

    sub_ids = _ids(notifications, "FORM_SUBMISSION")
    sub_forms = dict((await db.execute(
        select(FormSubmission.id, FormSubmission.form_id).where(FormSubmission.id.in_(sub_ids), FormSubmission.owner_id == agent_id)
    )).all()) if sub_ids else {}

    host_ids = _ids(notifications, "HOST_REQUEST")
    host_reqs = {r.id: r for r in (await db.execute(
        select(OpenHouseHostRequest).where(OpenHouseHostRequest.id.in_(host_ids))
    )).scalars().all()} if host_ids else {}

    # A message sent from a listing page was filed against the property, not the lead - match it to
    # the lead that the same message created (same agent, within a few minutes).
    inquiry_notifs = [n for n in notifications if n.type == "PROPERTY_INQUIRY" and n.reference_type == "PROPERTY"]
    inquiry_events = []
    if inquiry_notifs:
        earliest = min(n.created_at for n in inquiry_notifs) - timedelta(minutes=10)
        inquiry_events = (await db.execute(
            select(LeadEvent.lead_id, LeadEvent.occurred_at, Lead.name).join(Lead, Lead.id == LeadEvent.lead_id)
            .where(Lead.owner_id == agent_id, LeadEvent.source_type.in_(_INQUIRY_SOURCE_TYPES), LeadEvent.occurred_at >= earliest)
        )).all()

    today = date.today()
    for n in notifications:
        ref, kind = n.reference_id, n.reference_type
        lead_id = None
        if kind == "LEAD":
            lead_id = ref
        elif kind == "VISITOR":
            lead_id = visitor_leads.get(ref)
        elif kind == "TOUR":
            lead_id = tour_leads.get(ref)
        elif kind == "PROPERTY" and n.type == "PROPERTY_INQUIRY" and n.created_at:
            near = [e for e in inquiry_events if abs((e.occurred_at - n.created_at).total_seconds()) <= 600
                    and (not n.visitor_name or (e.name or "").strip().lower() == n.visitor_name.strip().lower())]
            if near:
                lead_id = min(near, key=lambda e: abs((e.occurred_at - n.created_at).total_seconds())).lead_id
        if lead_id:
            out[n.id] = f"/leads/{lead_id}"
        elif kind == "OPEN_HOUSE_FORM_REQUEST" and ref:
            # That request, open on the Open Houses page where it's marked Sent / Scheduled.
            out[n.id] = f"/open-houses?request={ref}"
        elif kind == "FORM_SUBMISSION" and ref in sub_forms:
            out[n.id] = f"/studio/forms?tab=results&formId={sub_forms[ref]}&submission={ref}"
        elif kind == "TASK" and ref:
            out[n.id] = f"/leads/tasks?task={ref}"
        elif kind == "REVIEW" and ref:
            out[n.id] = f"/studio/reviews?review={ref}"
        elif kind == "STUDIO_MEDIA_ASSET" and ref:
            out[n.id] = f"/studio/media/{ref}"  # that video's own page
        elif kind == "HOST_REQUEST" and ref in host_reqs:
            r = host_reqs[ref]
            if r.listing_owner_agent_id == agent_id:
                # That listing's hosts window - the request is right there to approve / answer.
                out[n.id] = f"/open-houses/my-listings?listingKey={r.listing_key}"
            elif r.status == "approved" and r.requested_date and r.requested_date >= today:
                out[n.id] = f"/open-houses?build={r.id}"  # straight into building the kit
            else:
                out[n.id] = f"/discovery?myRequests=1&request={r.id}"

    # A lead the agent has since deleted: open the Leads list, not a dead lead page.
    lead_links = {nid: href.split("/")[2].split("?")[0] for nid, href in
                  [(n.id, out.get(n.id) or n.link or "") for n in notifications] if href.startswith("/leads/")
                  and len(href.split("/")[2].split("?")[0]) == 36}
    if lead_links:
        alive = set((await db.execute(
            select(Lead.id).where(Lead.owner_id == agent_id, Lead.id.in_(set(lead_links.values())))
        )).scalars().all())
        for nid, lid in lead_links.items():
            if lid not in alive:
                out[nid] = "/leads"
    return out
