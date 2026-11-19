"""Day 6 follow-ups (11/19).
- A form's "do you have an agent?" answer sets the lead's agent status - Setup My Own Showcase included
  (its leads stayed "Not sure").
- Deleting a Showcase also cancels its waiting emails (the agent's "has an agent -> delete their Showcase?")."""
from types import SimpleNamespace

import pytest

from app.services.forms_service import _showcase_gate


def _form(t):
    return SimpleNamespace(form_type=t)


@pytest.mark.parametrize("form_type, answers, want", [
    ("showcase_request", {"has_agent": "No, I'm not working with an agent"}, "no"),
    ("showcase_request", {"has_agent": "Yes, I already have an agent"}, "yes"),
    ("showcase_request", {}, None),
    ("buying", {"sc_has_agent": "No, I'm not working with an agent"}, "no"),
    ("how_can_i_help", {"intent": "Buy a home", "buy_working_with_agent": "Yes"}, "yes"),
    ("how_can_i_help", {"intent": "Sell my home", "buy_working_with_agent": "Yes"}, None),
    ("selling", {"has_agent": "No"}, None),
])
def test_form_agent_answer(form_type, answers, want):
    assert _showcase_gate(_form(form_type), answers) == want


def test_delete_cancels_waiting_showcase_mail():
    """The cancel statement targets this Showcase's id, its open house thank-you, waiting rows only,
    this agent only - checked on the SQL it builds (no database needed)."""
    import asyncio
    import app.main  # noqa: F401  (load the app first - collections_service has an import-order loop on its own)
    from app.services.collections_service import CollectionsService

    seen = []

    class _Res:
        rowcount = 2

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _DB:
        def begin_nested(self):
            return _Nested()

        async def execute(self, stmt):
            seen.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            return _Res()

    col = SimpleNamespace(id="col-1", visitor_email=" Buyer@Example.com ")
    n = asyncio.run(CollectionsService._stop_showcase_mail(_DB(), col, "agent-1"))
    assert n == 2
    sql = seen[0]
    assert "UPDATE scheduled_emails" in sql and "CANCELLED" in sql
    assert "'PENDING'" in sql and "'DIGEST'" in sql and "'SENT'" not in sql
    assert "col-1" in sql and "visitor_confirmation" in sql and "buyer@example.com" in sql
    assert "showcase_ready" in sql and "showcase_shared" in sql
    assert "agent-1" in sql
    assert "UPDATE open_house_visitors" in seen[1] and "showcase_override" in seen[1]


def test_delete_mail_failure_never_blocks():
    import asyncio
    import app.main  # noqa: F401  (load the app first - collections_service has an import-order loop on its own)
    from app.services.collections_service import CollectionsService

    class _Boom:
        def begin_nested(self):
            raise RuntimeError("db hiccup")

    col = SimpleNamespace(id="col-1", visitor_email="a@example.com")
    assert asyncio.run(CollectionsService._stop_showcase_mail(_Boom(), col, "agent-1")) == 0
