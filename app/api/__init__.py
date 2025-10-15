from fastapi import APIRouter
from .auth_routes import router as auth_router
from .property_visit_routes import router as property_visit_router
from .collections_routes import router as collections_router
from .properties_routes import router as properties_router
from .collection_preferences_routes import router as collection_preferences_router
from .open_houses_routes import router as open_houses_router
from .subscription_routes import router as subscription_router
from .webhook_routes import router as webhook_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(property_visit_router)
router.include_router(collections_router)
router.include_router(properties_router)
router.include_router(collection_preferences_router)
router.include_router(open_houses_router)
router.include_router(subscription_router)
router.include_router(webhook_router)