from fastapi import APIRouter
from .auth_routes import router as auth_router
from .property_visit_routes import router as property_visit_router
from .collections_routes import router as collections_router
from .properties_routes import router as properties_router
from .collection_preferences_routes import router as collection_preferences_router
from .open_houses_routes import router as open_houses_router
from .guest_pass_routes import router as guest_pass_router
from .subscription_routes import router as subscription_router
from .webhook_routes import router as webhook_router
from .notifications_routes import router as notifications_router
from .admin_routes import router as admin_router
from .discover_routes import router as discover_router
from .agent_hub_routes import router as agent_hub_router
from .forms_routes import router as forms_router
from .open_house_manager_routes import router as open_house_manager_router
from .studio_routes import router as studio_router
from .studio_media_routes import router as studio_media_router
from .blog_routes import router as blog_router
from .market_data_routes import router as market_data_router
from .design_routes import router as design_router
from .lead_routes import router as lead_router
from .stats_routes import router as stats_router
from .cma_routes import router as cma_router
from .cma_backtest_routes import router as cma_backtest_router
from .seller_net_sheet_routes import router as seller_net_sheet_router
from .buyer_cash_needed_routes import router as buyer_cash_needed_router
from .ai_routes import router as ai_router
from .admin_ai_routes import router as admin_ai_router
from .admin_storage_routes import router as admin_storage_router
from .admin_activity_routes import router as admin_activity_router
from .admin_groups_routes import router as admin_groups_router
from .admin_promo_routes import router as admin_promo_router
from .site_routes import router as site_router
from .public_site_routes import router as public_site_router
from .property_website_routes import router as property_website_router
from .public_property_site_routes import router as public_property_site_router
from .support_routes import router as support_router
from .admin_support_routes import router as admin_support_router
from .saved_properties_routes import router as saved_properties_router
from .public_questions_routes import router as public_questions_router
from .admin_public_questions_routes import router as admin_public_questions_router
from .task_routes import router as task_router, calendar_router, public_router as public_followups_router
from .integrations_routes import router as integrations_router
from .setup_routes import router as setup_router
from .email_signature_routes import router as email_signature_router
from .email_avatar_routes import router as email_avatar_router
from .client_email_routes import router as client_email_router
from .qr_brand_routes import router as qr_brand_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(property_visit_router)
router.include_router(collections_router)
router.include_router(properties_router)
router.include_router(collection_preferences_router)
router.include_router(open_houses_router)
router.include_router(guest_pass_router)
router.include_router(subscription_router)
router.include_router(webhook_router)
router.include_router(notifications_router)
router.include_router(admin_router)
router.include_router(discover_router)
router.include_router(agent_hub_router)
router.include_router(forms_router)
router.include_router(open_house_manager_router)
router.include_router(studio_router)
router.include_router(studio_media_router)
router.include_router(blog_router)
router.include_router(market_data_router)
router.include_router(design_router)
router.include_router(lead_router)
router.include_router(stats_router)
router.include_router(cma_router)
router.include_router(cma_backtest_router)
router.include_router(seller_net_sheet_router)
router.include_router(buyer_cash_needed_router)
router.include_router(ai_router)
router.include_router(admin_ai_router)
router.include_router(admin_storage_router)
router.include_router(admin_activity_router)
router.include_router(admin_groups_router)
router.include_router(admin_promo_router)
router.include_router(site_router)
router.include_router(public_site_router)
router.include_router(property_website_router)
router.include_router(public_property_site_router)
router.include_router(support_router)
router.include_router(admin_support_router)
router.include_router(saved_properties_router)
router.include_router(public_questions_router)
router.include_router(admin_public_questions_router)
router.include_router(task_router)
router.include_router(calendar_router)
router.include_router(public_followups_router)
router.include_router(integrations_router)
router.include_router(setup_router)
router.include_router(email_signature_router)
router.include_router(email_avatar_router)
router.include_router(client_email_router)
router.include_router(qr_brand_router)
