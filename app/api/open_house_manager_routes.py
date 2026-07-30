from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.database import get_db
from app.models.database import User
from app.schemas.open_house_manager import (
    AddTeamMemberRequest,
    AgentSearchResult,
    BroadcastResponse,
    CounterHostRequestRequest,
    CreateBroadcastRequest,
    CreateHostRequestRequest,
    CreateHostRequestResult,
    HostRequestResponse,
    IncomingBroadcastResponse,
    ListingDetailResponse,
    ListingHostNoteResponse,
    ManualListingCreate,
    MyListingItem,
    MyListingsResponse,
    RefreshListingsResponse,
    TeamMemberResponse,
    UpdateListingNotesRequest,
)
from app.services import open_house_manager_service as service
from app.utils.auth import require_basic_plan, require_broker_authorization
from app.utils.property_sync_scheduler import run_global_property_sync
from app.config.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/open-house-manager", tags=["open-house-manager"], dependencies=[Depends(require_broker_authorization)])


@router.get("/my-listings", response_model=MyListingsResponse)
async def my_listings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.get_host_requests(db, current_user, status_filter)
    except Exception as e:
        logger.error(f"Failed to fetch host requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load host requests right now.")


@router.get("/my-requests", response_model=List[HostRequestResponse])
async def my_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.decline_host_request(db, current_user, request_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to decline host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to decline this request right now.")


@router.post("/host-requests/{request_id}/counter", response_model=HostRequestResponse)
async def counter_request(
    request_id: str,
    payload: CounterHostRequestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.respond_to_counter(db, current_user, request_id, accept=False)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to decline countered host request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to decline this time right now.")


@router.get("/team/search-agents", response_model=List[AgentSearchResult])
async def team_search_agents(
    q: str = Query(..., min_length=2, max_length=120),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.search_agents(db, current_user, q)
    except Exception as e:
        logger.error(f"Failed to search agents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to search agents right now.")


@router.get("/team/members", response_model=List[TeamMemberResponse])
async def team_members(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
):
    try:
        await service.remove_team_member(db, current_user, member_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to remove team member: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to remove this agent right now.")


@router.get("/team/broadcasts/mine", response_model=List[BroadcastResponse])
async def my_broadcasts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.list_my_broadcasts(db, current_user)
    except Exception as e:
        logger.error(f"Failed to list my broadcasts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to load your broadcasts right now.")


@router.get("/team/broadcasts/incoming", response_model=List[IncomingBroadcastResponse])
async def incoming_broadcasts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.respond_to_broadcast(db, current_user, broadcast_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to respond to broadcast: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to send your response right now.")


@router.delete("/team/broadcasts/{broadcast_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_broadcast(
    broadcast_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
):
    try:
        return await service.save_listing_notes(db, current_user, listing_key, payload)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to save listing notes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unable to save these notes right now.")


@router.post("/my-listings/manual", response_model=MyListingItem, status_code=status.HTTP_201_CREATED)
async def add_manual_listing(
    payload: ManualListingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    current_user: User = Depends(require_basic_plan),
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
    background_tasks.add_task(run_global_property_sync, max_pages=5)
    return RefreshListingsResponse(status="started")
