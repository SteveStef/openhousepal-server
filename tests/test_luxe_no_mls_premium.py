"""No-MLS Premium gets Luxe and the QR center (No-MLS plans can never buy IDX). No-MLS Basic stays
locked and is only ever pointed at No-MLS Premium; Basic and Premium still point at IDX."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.qr_brand_routes import QrBrandUpdate, qr_brand_for, set_my_qr_brand
from app.services.ai.ai_actions import ActionError
from app.services.ai.ai_actions_catalog import form_style
from app.utils.auth import luxe_blog_theme_allowed, luxe_theme_allowed, luxe_upgrade_label


def _user(tier, **extra):
    return SimpleNamespace(id="u1", plan_tier=tier, subscription_status="ACTIVE", is_admin=False, qr_logo=None,
                           first_name="Pat", last_name="Lee", **extra)


class _Db:
    def __init__(self, user):
        self.user = user

    async def get(self, _model, _id):
        return self.user


@pytest.mark.parametrize("tier,allowed", [("PRO_NO_MLS", True), ("IDX", True), ("NO_MLS", False), ("BASIC", False), ("PREMIUM", False)])
def test_luxe_follows_the_plan(tier, allowed):
    assert luxe_theme_allowed(_user(tier)) is allowed
    assert luxe_blog_theme_allowed(_user(tier)) is allowed


@pytest.mark.parametrize("tier,allowed", [("PRO_NO_MLS", True), ("IDX", True), ("NO_MLS", False), ("BASIC", False), ("PREMIUM", False)])
def test_qr_center_follows_the_plan(tier, allowed):
    assert asyncio.run(qr_brand_for(None, _user(tier)))["allowed"] is allowed


@pytest.mark.parametrize("tier,label", [("NO_MLS", "No-MLS Premium"), ("BASIC", "IDX"), ("PREMIUM", "IDX")])
def test_the_lock_names_the_plan_on_their_track(tier, label):
    assert luxe_upgrade_label(_user(tier)) == label
    with pytest.raises(ActionError) as luxe:
        form_style(_user(tier), look="luxe")
    assert str(luxe.value) == f"The Luxe look comes with the {label} plan."
    user = _user(tier)
    with pytest.raises(HTTPException) as qr:
        asyncio.run(set_my_qr_brand(QrBrandUpdate(choice="initials"), db=_Db(user), current_user=user))
    assert qr.value.status_code == 403 and label in qr.value.detail


def test_no_mls_premium_can_pick_luxe_in_chat():
    assert form_style(_user("PRO_NO_MLS"), look="luxe") == {"theme": "luxe"}
