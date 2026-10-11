"""Admin-only agent storage analytics - who's using how much media storage, broken down by
category (AI Media, Design Studio, blogs, brand logos, listing/agent photos) and by month, so
usage can be watched against each tier's storage cap. See storage_ledger_service.py."""
from typing import List, Optional

from pydantic import BaseModel


class AdminStorageCategoryStat(BaseModel):
    category: str
    bytes: int
    count: int


class AdminStorageMonthlyPoint(BaseModel):
    month: str  # YYYY-MM
    bytes_added: int


class AdminStorageAgentSummary(BaseModel):
    owner_id: str
    name: str
    email: str
    plan_tier: Optional[str] = None
    used_bytes: int
    cap_bytes: int
    item_count: int


class AdminStorageAgentDetail(AdminStorageAgentSummary):
    by_category: List[AdminStorageCategoryStat] = []
    monthly_trend: List[AdminStorageMonthlyPoint] = []  # last 12 months of bytes uploaded, oldest first
