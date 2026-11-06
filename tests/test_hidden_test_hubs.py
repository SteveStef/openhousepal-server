"""Our own test accounts' public pages stay hidden: they showed a real brokerage's name (owner, 11/06)."""
import pytest
from fastapi import HTTPException

from app.api.agent_hub_routes import HIDDEN_TEST_HUBS, _resolve_public_hub


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", sorted(HIDDEN_TEST_HUBS) + ["Admin-User"])
async def test_hidden_hub_is_not_found_before_any_lookup(slug):
    with pytest.raises(HTTPException) as e:
        await _resolve_public_hub(slug, None)  # no database: it must stop first
    assert e.value.status_code == 404
