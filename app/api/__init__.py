from fastapi import APIRouter
from .routes import router as property_router
from .auth_routes import router as auth_router
from .open_house_routes import router as open_house_router
from .collections_routes import router as collections_router
from .properties_routes import router as properties_router
from .open_houses_routes import router as open_houses_router

router = APIRouter()
router.include_router(property_router)
router.include_router(auth_router)
router.include_router(open_house_router)
router.include_router(collections_router)
router.include_router(properties_router)
router.include_router(open_houses_router)