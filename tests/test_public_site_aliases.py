from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.api import public_site_routes
from app.services.collections_service import CollectionsService


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ScalarsResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _FakeDb:
    def __init__(self, value):
        self.value = value
        self.statements = []
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.value)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


class _FakeAreaDb:
    def __init__(self, areas):
        self.areas = areas
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarsResult(self.areas)


class _QueuedFakeDb:
    def __init__(self, values):
        self.values = list(values)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _ScalarResult(self.values.pop(0) if self.values else None)


@pytest.mark.asyncio
async def test_site_showcase_alias_filters_by_site_owner_public_active(monkeypatch):
    async def fake_owner(_db, site_id):
        return SimpleNamespace(id=site_id), SimpleNamespace(id="agent-a")

    monkeypatch.setattr(public_site_routes, "_published_site_owner", fake_owner)
    db = _FakeDb("collection-a")

    await public_site_routes._assert_site_showcase(db, "site-a", "share-token")

    compiled = str(db.statements[0])
    assert "collections.share_token" in compiled
    assert "collections.owner_id" in compiled
    assert "collections.is_public" in compiled
    assert "collections.status" in compiled


@pytest.mark.asyncio
async def test_site_showcase_alias_404s_without_owned_public_collection(monkeypatch):
    async def fake_owner(_db, site_id):
        return SimpleNamespace(id=site_id), SimpleNamespace(id="agent-a")

    monkeypatch.setattr(public_site_routes, "_published_site_owner", fake_owner)

    with pytest.raises(HTTPException) as exc:
        await public_site_routes._assert_site_showcase(_FakeDb(None), "site-a", "other-agent-token")

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


def test_showcase_request_examples_only_use_real_sold_sales():
    examples = public_site_routes._showcase_example_properties({
        "recentSales": [
            {"address": "1 Main St", "status": "Active", "price": 900000},
            {"address": "2 Main St", "status": "Closed", "price": 95000},
            {"address": "3 Main St", "status": "Sold", "price": 750000, "city": "Ardmore"},
        ]
    })

    assert examples is not None
    assert len(examples) == 1
    assert examples[0].address == "3 Main St"


@pytest.mark.asyncio
async def test_ohp_shared_showcase_query_requires_active_status():
    db = _FakeDb(None)

    result = await CollectionsService.get_shared_collection(db, "inactive-token")

    assert result is None
    compiled = str(db.statements[0])
    assert "collections.share_token" in compiled
    assert "collections.is_public" in compiled
    assert "collections.status" in compiled


def _fake_public_form():
    return SimpleNamespace(
        id="form-a",
        owner_id="agent-a",
        form_type="custom",
        slug="contact",
        title="Contact",
        headline=None,
        subtitle=None,
        questions=[],
        success_heading="Thanks",
        success_message=None,
        accent_color=None,
        show_branding=True,
        dark_mode=False,
        redirect_to_agent_hub=True,
        property_photo_carousel=False,
        branding_layout="split",
        linked_property=None,
    )


@pytest.mark.asyncio
async def test_site_form_alias_accepts_slug_or_legacy_id(monkeypatch):
    async def fake_owner(_db, site_id):
        return SimpleNamespace(id=site_id), SimpleNamespace(id="agent-a")

    monkeypatch.setattr(public_site_routes, "_published_site_owner", fake_owner)
    form = _fake_public_form()
    db = _FakeDb(form)

    _, user, resolved = await public_site_routes._get_site_form(db, "site-a", "contact")

    assert user.id == "agent-a"
    assert resolved is form
    compiled = str(db.statements[0])
    assert "agent_forms.id" in compiled
    assert "agent_forms.slug" in compiled
    assert "agent_forms.owner_id" in compiled
    assert "agent_forms.is_enabled" in compiled


@pytest.mark.asyncio
async def test_site_form_metadata_fetch_does_not_track_view(monkeypatch):
    async def fake_get_site_form(_db, site_id, form_id):
        return SimpleNamespace(id=site_id), SimpleNamespace(
            id="agent-a",
            first_name="Ada",
            last_name="Agent",
            brokerage="OHP Realty",
            photo_url=None,
            professional_title=None,
            market=None,
        ), _fake_public_form()

    monkeypatch.setattr(public_site_routes, "_get_site_form", fake_get_site_form)
    db = _FakeDb(None)

    response = await public_site_routes.get_site_public_form("site-a", "form-a", track_view=False, c=None, db=db)

    assert db.added == []
    assert db.commits == 0
    assert response.background_effects is True
    assert response.welcome_text_effects is True


@pytest.mark.asyncio
async def test_site_form_real_render_tracks_one_view(monkeypatch):
    async def fake_get_site_form(_db, site_id, form_id):
        return SimpleNamespace(id=site_id), SimpleNamespace(
            id="agent-a",
            first_name="Ada",
            last_name="Agent",
            brokerage="OHP Realty",
            photo_url=None,
            professional_title=None,
            market=None,
        ), _fake_public_form()

    monkeypatch.setattr(public_site_routes, "_get_site_form", fake_get_site_form)
    db = _FakeDb(None)

    await public_site_routes.get_site_public_form("site-a", "form-a", track_view=True, c=None, db=db)

    assert len(db.added) == 1
    assert db.added[0].event_type == "form_view"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_site_showcase_creator_resolves_locked_form_from_site_owner(monkeypatch):
    async def fake_owner(_db, site_id):
        return SimpleNamespace(id=site_id), SimpleNamespace(id="agent-a")

    monkeypatch.setattr(public_site_routes, "_published_site_owner", fake_owner)
    form = SimpleNamespace(id="showcase-form", owner_id="agent-a", form_type="showcase_request", is_enabled=True)
    db = _QueuedFakeDb([form])

    _, user, resolved_form = await public_site_routes._get_site_showcase_creator_form(db, "site-a")

    assert user.id == "agent-a"
    assert resolved_form is form
    compiled = str(db.statements[0])
    assert "agent_forms.owner_id" in compiled
    assert "agent_forms.form_type" in compiled
    assert "agent_forms.is_enabled" in compiled


@pytest.mark.asyncio
async def test_site_showcase_creator_submit_uses_existing_form_pipeline(monkeypatch):
    form = SimpleNamespace(id="showcase-form", owner_id="agent-a", form_type="showcase_request")
    payload = SimpleNamespace(answers=[])
    called = {}

    async def fake_creator_form(_db, site_id):
        called["site_id"] = site_id
        return SimpleNamespace(id=site_id), SimpleNamespace(id="agent-a"), form

    async def fake_submit(_db, submitted_form, submitted_payload):
        called["form"] = submitted_form
        called["payload"] = submitted_payload
        return SimpleNamespace(success=True, message="ok", submission_id="submission-a", redirect_url="/showcase/token-a")

    monkeypatch.setattr(public_site_routes, "_get_site_showcase_creator_form", fake_creator_form)
    monkeypatch.setattr(public_site_routes.forms_service, "submit_form", fake_submit)

    response = await public_site_routes.submit_site_showcase_creator("site-a", payload, request=SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.9")), db=_FakeDb(None))

    assert called == {"site_id": "site-a", "form": form, "payload": payload}
    assert response.redirect_url == "/showcase/token-a"


def _fake_area(slug, name):
    return SimpleNamespace(
        id=slug,
        slug=slug,
        name=name,
        state="PA",
        county="Montgomery",
        area_type="city",
        parent_area_name=None,
        zip_codes=["19010"],
        geography={},
        facts={
            "public_schools": {"districts": [{"name": f"{name} SD"}]},
            "restaurants": {"entities": [{"name": f"{name} Cafe"}]},
        },
        narrative={"summary": f"About {name}."},
    )


@pytest.mark.asyncio
async def test_site_area_compare_uses_only_owned_selected_areas(monkeypatch):
    async def fake_owner(_db, site_id):
        return SimpleNamespace(id=site_id, owner_id="agent-a"), SimpleNamespace(id="agent-a")

    async def fake_stats(_db, _scope, _fallback):
        return {"active_count": 2, "median_price": 700000}

    async def fake_inventory(_db, _scope, limit=3, **_kwargs):
        return ([{"listing_key": "listing-a", "photo": "photo.jpg"}][:limit], 2)

    monkeypatch.setattr(public_site_routes, "_published_site_owner", fake_owner)
    monkeypatch.setattr(public_site_routes.content_adapters, "get_area_live_market_stats", fake_stats)
    monkeypatch.setattr(public_site_routes.content_adapters, "get_area_inventory", fake_inventory)

    # Deliberately reversed DB order; the response should preserve the URL/share order.
    db = _FakeAreaDb([_fake_area("rittenhouse-square-pa", "Rittenhouse Square"), _fake_area("villanova-pa", "Villanova")])

    response = await public_site_routes.compare_site_areas("site-a", "villanova-pa,rittenhouse-square-pa", db)

    assert [area["slug"] for area in response["areas"]] == ["villanova-pa", "rittenhouse-square-pa"]
    assert response["areas"][0]["inventory"]["total"] == 2
    assert response["areas"][0]["geography_type"] == "zip_city"
    compiled = str(db.statements[0])
    assert "agent_market_areas.owner_id" in compiled
    assert "market_areas.slug" in compiled


@pytest.mark.asyncio
async def test_site_area_compare_404s_for_unselected_area(monkeypatch):
    async def fake_owner(_db, site_id):
        return SimpleNamespace(id=site_id, owner_id="agent-a"), SimpleNamespace(id="agent-a")

    monkeypatch.setattr(public_site_routes, "_published_site_owner", fake_owner)

    with pytest.raises(HTTPException) as exc:
        await public_site_routes.compare_site_areas("site-a", "villanova-pa,unserved-pa", _FakeAreaDb([_fake_area("villanova-pa", "Villanova")]))

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


def test_site_area_compare_requires_two_distinct_neighborhoods():
    with pytest.raises(HTTPException) as exc:
        public_site_routes._parse_compare_slugs("villanova-pa,villanova-pa")

    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
