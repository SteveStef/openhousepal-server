"""Admin-only CMA accuracy test (see app/services/cma_backtest_service.py)."""
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import CmaBacktestBatch, User
from app.services import cma_backtest_service as svc
from app.utils.auth import require_admin_user

router = APIRouter(prefix="/admin/cma-backtest", tags=["admin"])


class BacktestSettings(BaseModel):
    towns: List[str] = []
    random_spread: bool = False
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    property_type: str = "single_family"
    count: int = Field(10, ge=1, le=svc.MAX_HOMES_PER_BATCH)
    mode: str = "buyer"
    dry_run: bool = False
    cost_cap_usd: float = Field(svc.DEFAULT_COST_CAP_USD, gt=0, le=500)


class StartRequest(BacktestSettings):
    # The exact homes shown in the preview, so what the admin approved is what runs.
    listing_keys: List[str] = []


def _batch_out(b: CmaBacktestBatch, with_items: bool = True) -> dict:
    items = b.items or []
    out = {
        "id": b.id, "status": b.status, "settings": b.settings, "error": b.error,
        "estimated_cost_usd": b.estimated_cost_usd, "actual_cost_usd": b.actual_cost_usd,
        "created_at": b.created_at, "updated_at": b.updated_at,
        "counts": {s: sum(1 for i in items if i.get("status") == s) for s in ("queued", "running", "done", "failed", "leak", "interrupted")},
        "summary": svc.summarize(items),
        "worker_running": svc.worker_running(),
    }
    if with_items:
        out["items"] = [{k: v for k, v in i.items() if k not in ("review", "ai_input_excerpt")} for i in items]
    return out


@router.get("/options")
async def options(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    return {
        "towns": await svc.town_options(db),
        "property_types": [{"key": k, "label": lbl} for k, (lbl, _) in svc.PROPERTY_TYPES.items()],
        "cost": await svc.cost_per_home_estimate(db),
        "default_cost_cap_usd": svc.DEFAULT_COST_CAP_USD,
        "max_homes": svc.MAX_HOMES_PER_BATCH,
        "charged_to": {"email": admin.email, "plan_tier": admin.plan_tier},
    }


@router.post("/preview")
async def preview(req: BacktestSettings, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    homes = await svc.sample_homes(db, towns=req.towns, random_spread=req.random_spread, price_min=req.price_min,
                                   price_max=req.price_max, property_type=req.property_type, count=req.count)
    cost = await svc.cost_per_home_estimate(db)
    est = 0.0 if req.dry_run else round(cost["per_home_with_margin_usd"] * len(homes), 2)
    return {"homes": homes, "estimated_cost_usd": est, "per_home_usd": cost["per_home_with_margin_usd"],
            "within_cap": est <= req.cost_cap_usd}


@router.post("/batches")
async def start(req: StartRequest, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    homes = await svc.sample_homes(db, towns=req.towns, random_spread=req.random_spread, price_min=req.price_min,
                                   price_max=req.price_max, property_type=req.property_type,
                                   count=max(req.count, len(req.listing_keys)) * (3 if req.listing_keys else 1))
    if req.listing_keys:
        by_key = {h["listing_key"]: h for h in homes}
        missing = [k for k in req.listing_keys if k not in by_key]
        if missing:
            # Re-sampling is random - fetch the previewed homes directly.
            from app.models.database import Property
            lag = await svc._median_contract_to_close_days(db)
            events = await svc.price_events(db, missing)
            for p in (await db.execute(select(Property).where(Property.listing_key.in_(missing)))).scalars():
                facts = svc.as_of_facts(p, lag, events.get(p.listing_key))
                if facts:
                    by_key[p.listing_key] = {
                        "listing_key": p.listing_key, "address": p.street_address, "city": p.city, "state": p.state,
                        "zipcode": p.zipcode, "property_type": p.home_type, "sqft": p.living_area, "year_built": p.year_built,
                        "segment": {"town": (p.city or "").title(), "price_band": svc.price_band(facts["close_price"]),
                                    "type": next((lbl for _, (lbl, ts) in svc.PROPERTY_TYPES.items() if p.home_type in ts), p.home_type)},
                        **facts, "status": "queued"}
        homes = [by_key[k] for k in req.listing_keys if k in by_key]
    homes = homes[:svc.MAX_HOMES_PER_BATCH]
    if not homes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No recent closed sales match those settings.")
    cost = await svc.cost_per_home_estimate(db)
    est = 0.0 if req.dry_run else round(cost["per_home_with_margin_usd"] * len(homes), 2)
    if est > req.cost_cap_usd:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Estimated cost ${est:.2f} is over the ${req.cost_cap_usd:.2f} cap - test fewer homes or raise the cap.")
    batch = CmaBacktestBatch(
        owner_id=admin.id, status="queued", items=homes, estimated_cost_usd=est, actual_cost_usd=0.0,
        settings={**req.model_dump(exclude={"listing_keys"}), "per_home_estimate_usd": 0.0 if req.dry_run else cost["per_home_with_margin_usd"]},
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    svc.ensure_worker()
    return _batch_out(batch)


@router.get("/batches")
async def list_batches(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    rows = (await db.execute(select(CmaBacktestBatch).where(CmaBacktestBatch.owner_id == admin.id)
                             .order_by(CmaBacktestBatch.created_at.desc()))).scalars().all()
    return [_batch_out(b, with_items=False) for b in rows]


async def _owned(db, admin, batch_id) -> CmaBacktestBatch:
    b = await db.get(CmaBacktestBatch, batch_id)
    if not b or b.owner_id != admin.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found.")
    return b


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    return _batch_out(await _owned(db, admin, batch_id))


@router.get("/batches/{batch_id}/review")
async def review_bundle(batch_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    """Everything the word-for-word review needs, for every finished home, in one download."""
    b = await _owned(db, admin, batch_id)
    body = json.dumps({"batch": _batch_out(b, with_items=False), "homes": b.items}, default=str, indent=1)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="cma-accuracy-test-{b.id[:8]}.json"'})


@router.post("/batches/{batch_id}/stop")
async def stop(batch_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    b = await _owned(db, admin, batch_id)
    if b.status in ("queued", "running"):
        b.status = "stopped"
        b.error = "Stopped by admin (the home in progress finishes first)."
        await db.commit()
    return _batch_out(b)


@router.post("/batches/{batch_id}/resume")
async def resume(batch_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    b = await _owned(db, admin, batch_id)
    if b.status in ("stopped", "running", "queued"):
        b.status = "queued"
        b.error = None
        await db.commit()
        svc.ensure_worker()
    return _batch_out(b)


@router.delete("/batches/{batch_id}", status_code=204)
async def delete_batch(batch_id: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    b = await _owned(db, admin, batch_id)
    if b.status == "running" and svc.worker_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the test first.")
    ids = [i.get("report_id") for i in (b.items or []) if i.get("report_id")]
    await svc.delete_batch_reports(db, admin.id, ids)
    await db.delete(b)
    await db.commit()


@router.delete("/reports")
async def delete_all_test_reports(db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin_user)):
    """Bulk delete every accuracy-test CMA report (the scoreboards stay; their report links go dead)."""
    if svc.worker_running():
        raise HTTPException(status.HTTP_409_CONFLICT, "A test is running - stop it first.")
    n = await svc.delete_batch_reports(db, admin.id)
    await db.commit()
    return {"deleted": n}
