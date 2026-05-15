from fastapi import APIRouter, Depends, HTTPException
import os
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import User
from dotenv import load_dotenv
from typing import Optional
from app.database import get_db
from app.config.logging import get_logger
from app.utils.auth import get_current_active_user, require_basic_plan, require_broker_authorization

logger = get_logger(__name__)

load_dotenv()

router = APIRouter()

class GetDiscoveryResponse:
    Street: str
    City: str
    State: str
    Zipcode: str
    Price: str
    Image: str
    Beds: int
    Baths: int
    SqureFeet: int
    ListAgentFullName:str
    ListAgentPreferredPhone: str
    ListOfficeName: str
    ListOfficePhone: str
    New: bool # Just added within 3 days ago or something
    DistanceFromLandmark: Optional[float] # only if they have a landmark defined


@router.post("/discovery", response_model=GetDiscoveryResponse, dependencies=[Depends(require_broker_authorization)])
async def get_discovery(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_basic_plan)
):
    # Get the discovery preferences for this user
    # Make a database query to get all properties with those fileters and return the results
    # Each user should have default preferences based off the brokerage they're from
    pass
