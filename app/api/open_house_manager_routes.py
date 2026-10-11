from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from datetime import date

from app.database import get_db
from app.models.database import User, PlanTier
from app.schemas.open_house import VisitorResponse
from app.schemas.open_house_manager import (
    ActionCountsResponse,
    AddTeamMemberRequest,
    AgentSearchResult,
    BroadcastResponse,
    CancelApprovedOpenRequest,
    CounterBackRequest,
    CounterHostRequestRequest,
    CreateBroadcastRequest,
    CreateHostRequestRequest,
    CreateHostRequestResult,
    HostRequestEventResponse,
    HostRequestResponse,
    IncomingBroadcastResponse,
    IncomingTeamInviteResponse,
    ListingDetailResponse,
    ListingHostNoteResponse,
    ManualListingCreate,
    MyListingItem,
    MyListingsResponse,
    RefreshListingsResponse,
    RespondToBroadcastRequest,
    TeamMemberResponse,
    TeamMembershipResponse,
    TrustedHostResponse,
    UpdateListingNotesRequest,
    UpdateRestrictionRequest,
)
from app.services import open_house_manager_service as service
from app.services import s3_service
from app.utils.auth import require_mls_access, require_plan_tier, require_broker_authorization
from app.utils.image_validation import assert_valid_jpeg
from app.utils.property_sync_scheduler import run_global_property_sync, run_open_house_sync
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/open-house-manager", tags=["open-house-manager"], dependencies=[Depends(require_broker_authorization)])

# JPG only, same restriction and size ceiling as every other photo upload in the app (agent
# profile/sale photos, Studio media) - see agent_hub_routes.py's PHOTO_CONTENT_TYPES for why.
MANUAL_LISTING_PHOTO_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
}
MANUAL_LISTING_PHOTO_MAX_BYTES = 5 * 1024 * 1024


@router.get("/my-listings", response_model=MyListingsResponse)
async def my_listings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    try:
        return await service.get_my_listings(db, current_user)
    except Exception as e:
        logger.error(f"Failed to fetch my listings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your listings right now.")


@router.get("/listing-status")
async def listing_status(
    listing_keys: str = Query(..., description="Comma-separated listing keys"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    """Used by Discovery to flag which browsed listings already have an open house scheduled."""
    try:
        keys = [k.strip() for k in listing_keys.split(",") if k.strip()]
        return await service.get_listing_open_house_statuses(db, keys)
    except Exception as e:
        logger.error(f"Failed to fetch listing statuses: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load listing statuses right now.")


@router.get("/host-requests", response_model=List[HostRequestResponse])
async def host_requests(
    status_filter: Optional[str] = Query(default="pending", alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.get_host_requests(db, current_user, status_filter)
    except Exception as e:
        logger.error(f"Failed to fetch host requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load host requests right now.")


@router.get("/action-counts", response_model=ActionCountsResponse)
async def action_counts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    """Live counts backing the red-dot 'action needed' badges - derived from actual request
    status so they clear the moment a request is approved/declined/countered/responded to."""
    try:
        return await service.get_action_counts(db, current_user)
    except Exception as e:
        logger.error(f"Failed to fetch action counts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load action counts right now.")


@router.get("/my-requests", response_model=List[HostRequestResponse])
async def my_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.get_my_requests(db, current_user)
    except Exception as e:
        logger.error(f"Failed to fetch my requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your requests right now.")


@router.post("/requests", response_model=CreateHostRequestResult, status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: CreateHostRequestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.create_host_request(db, current_user, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to submit this request right now.")


@router.post("/host-requests/{request_id}/approve", response_model=HostRequestResponse)
async def approve_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.approve_host_request(db, current_user, request_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to approve host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to approve this request right now.")


@router.post("/host-requests/{request_id}/decline", response_model=HostRequestResponse)
async def decline_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.decline_host_request(db, current_user, request_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to decline host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to decline this request right now.")


@router.post("/listings/{listing_key}/cancel-approved")
async def cancel_approved_open(
    listing_key: str,
    payload: CancelApprovedOpenRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        await service.cancel_approved_open(db, current_user, listing_key, payload)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to cancel approved open house: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to cancel this open house right now.")


@router.post("/host-requests/{request_id}/counter", response_model=HostRequestResponse)
async def counter_request(
    request_id: str,
    payload: CounterHostRequestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.counter_host_request(db, current_user, request_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to counter host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to propose a new time right now.")


@router.post("/host-requests/{request_id}/counter/accept", response_model=HostRequestResponse)
async def accept_counter_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.respond_to_counter(db, current_user, request_id, accept=True)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to accept countered host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to confirm this time right now.")


@router.post("/host-requests/{request_id}/counter/decline", response_model=HostRequestResponse)
async def decline_counter_request(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.respond_to_counter(db, current_user, request_id, accept=False)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to decline countered host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to decline this time right now.")


@router.post("/host-requests/{request_id}/counter-back", response_model=HostRequestResponse)
async def counter_back_request(
    request_id: str,
    payload: CounterBackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.counter_back_host_request(db, current_user, request_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to counter back on host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to propose a new time right now.")


@router.get("/host-requests/{request_id}/history", response_model=List[HostRequestEventResponse])
async def request_history(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.get_request_history(db, current_user, request_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to fetch host request history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load this request's history right now.")


@router.get("/team/search-agents", response_model=List[AgentSearchResult])
async def team_search_agents(
    q: str = Query(..., min_length=2, max_length=120),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.search_agents(db, current_user, q)
    except Exception as e:
        logger.error(f"Failed to search agents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to search agents right now.")


@router.get("/team/members", response_model=List[TeamMemberResponse])
async def team_members(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.list_team_members(db, current_user)
    except Exception as e:
        logger.error(f"Failed to list team members: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your team right now.")


@router.post("/team/members", response_model=TeamMemberResponse, status_code=status.HTTP_201_CREATED)
async def add_team_member(
    payload: AddTeamMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.add_team_member(db, current_user, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to add team member: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to add this agent right now.")


@router.delete("/team/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        await service.remove_team_member(db, current_user, member_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to remove team member: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to remove this agent right now.")


@router.get("/team/memberships", response_model=List[TeamMembershipResponse])
async def my_team_memberships(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.list_my_team_memberships(db, current_user)
    except Exception as e:
        logger.error(f"Failed to fetch team memberships: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your teams right now.")


@router.get("/team/invites/incoming", response_model=List[IncomingTeamInviteResponse])
async def incoming_team_invites(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.list_incoming_team_invites(db, current_user)
    except Exception as e:
        logger.error(f"Failed to fetch incoming team invites: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your team invites right now.")


@router.post("/team/invites/{member_id}/accept", response_model=IncomingTeamInviteResponse)
async def accept_team_invite(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.respond_to_team_invite(db, current_user, member_id, accept=True)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to accept team invite: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to accept this invite right now.")


@router.post("/team/invites/{member_id}/decline", response_model=IncomingTeamInviteResponse)
async def decline_team_invite(
    member_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.respond_to_team_invite(db, current_user, member_id, accept=False)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to decline team invite: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to decline this invite right now.")


@router.put("/team/restriction", status_code=status.HTTP_204_NO_CONTENT)
async def update_restriction(
    payload: UpdateRestrictionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        await service.update_restriction(db, current_user, payload)
    except Exception as e:
        logger.error(f"Failed to update host request restriction: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to save this setting right now.")


@router.get("/team/hosted-before", response_model=List[TrustedHostResponse])
async def hosted_before(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.get_trusted_hosts(db, current_user)
    except Exception as e:
        logger.error(f"Failed to fetch trusted hosts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your previous hosts right now.")


@router.get("/events/{event_id}/guests", response_model=List[VisitorResponse])
async def shared_guests(
    event_id: str,
    window_date: Optional[date] = Query(None, alias="date", description="Only that hosted day's sign-ins (one evergreen kit covers every date)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.get_shared_guests(db, current_user, event_id, window_date)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to fetch shared guests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load guest details right now.")


@router.get("/team/broadcasts/mine", response_model=List[BroadcastResponse])
async def my_broadcasts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.list_my_broadcasts(db, current_user)
    except Exception as e:
        logger.error(f"Failed to list my broadcasts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your broadcasts right now.")


@router.get("/team/broadcasts/incoming", response_model=List[IncomingBroadcastResponse])
async def incoming_broadcasts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.list_incoming_broadcasts(db, current_user)
    except Exception as e:
        logger.error(f"Failed to list incoming broadcasts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load team requests right now.")


@router.post("/team/broadcasts", response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    payload: CreateBroadcastRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.create_broadcast(db, current_user, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to create broadcast: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to send this request to your team right now.")


@router.post("/team/broadcasts/{broadcast_id}/respond", response_model=HostRequestResponse)
async def respond_to_broadcast(
    broadcast_id: str,
    payload: RespondToBroadcastRequest = RespondToBroadcastRequest(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.respond_to_broadcast(db, current_user, broadcast_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to respond to broadcast: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to send your response right now.")


@router.delete("/team/broadcasts/{broadcast_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        await service.cancel_broadcast(db, current_user, broadcast_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to cancel broadcast: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to cancel this broadcast right now.")


@router.get("/listings/{listing_key}", response_model=ListingDetailResponse)
async def listing_detail(
    listing_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.get_listing_detail(db, current_user, listing_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to fetch listing detail: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load this listing right now.")


@router.put("/listings/{listing_key}/notes", response_model=ListingHostNoteResponse)
async def update_listing_notes(
    listing_key: str,
    payload: UpdateListingNotesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_plan_tier(PlanTier.BASIC)),
):
    try:
        return await service.save_listing_notes(db, current_user, listing_key, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to save listing notes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to save these notes right now.")


@router.post("/my-listings/manual/photo")
async def upload_manual_listing_photo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    """Uploads a photo for a manually-added listing to S3 and returns its URL - the only way a
    photo reaches create_manual_listing below, which now only accepts an S3 URL for image_url."""
    extension = MANUAL_LISTING_PHOTO_CONTENT_TYPES.get(file.content_type)
    if not extension:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Please upload a JPG image.")

    contents = await file.read()
    if len(contents) > MANUAL_LISTING_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Photo must be 5MB or smaller.")
    try:
        assert_valid_jpeg(contents)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    from app.services import storage_ledger_service
    from app.services.ai.ai_config_service import ai_config_service
    _cfg = await ai_config_service.get_config(db)
    await storage_ledger_service.enforce_cap(db, current_user.id, _cfg, getattr(current_user, "plan_tier", None), len(contents))
    photo_url = s3_service.upload_bytes(contents, "manual-listing-photos", extension, file.content_type)
    await storage_ledger_service.record(db, current_user.id, "listing_photo", photo_url, len(contents))
    return {"photo_url": photo_url}


@router.post("/my-listings/manual", response_model=MyListingItem, status_code=status.HTTP_201_CREATED)
async def add_manual_listing(
    payload: ManualListingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    try:
        return await service.create_manual_listing(db, current_user, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to add manual listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to add this listing right now.")


@router.delete("/my-listings/manual/{listing_key}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_manual_listing(
    listing_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    try:
        await service.delete_manual_listing(db, current_user, listing_key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to remove manual listing: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to remove this listing right now.")


@router.post("/refresh-listings", response_model=RefreshListingsResponse)
async def refresh_listings(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_mls_access),
):
    """Kicks off the same incremental Bright MLS sync the nightly job runs, on demand, in the
    background - so an agent doesn't have to wait for the scheduled cron for their listings (or a
    listing they were just added as co-agent on) to show up in My Listings. Rate-limited (see
    MANUAL_REFRESH_COOLDOWN_SECONDS) since this is a shared external API call, not something scoped
    to one agent's clicks."""
    allowed, seconds_remaining = await service.try_start_manual_refresh(db)
    if not allowed:
        minutes_remaining = max(1, seconds_remaining // 60)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"A refresh was already started recently. Try again in about {minutes_remaining} minute{'s' if minutes_remaining != 1 else ''}.",
        )
    # Capped so a single click can't turn into an unbounded sync on a stale/never-run checkpoint -
    # each page is 200 properties, so 5 pages is at most 1,000 properties per click. If there's a
    # bigger backlog than that, the checkpoint still advances as far as this run got, so clicking
    # again (after the cooldown) continues from there instead of restarting.
    background_tasks.add_task(_refresh_listings_and_open_houses)
    return RefreshListingsResponse(status="started")


async def _refresh_listings_and_open_houses():
    """Listings first, then their open house schedule (a separate Bright resource with its own
    30-minute job) - so a click also picks up an open house the agent just put into Bright.
    Both capped at 5 pages (1,000 records) per click, same reasoning as above."""
    await run_global_property_sync(max_pages=5)
    await run_open_house_sync(max_pages=5)
