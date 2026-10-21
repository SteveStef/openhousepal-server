"""Checking off a disclosure follow-up marks the request Sent (user, 10/21) - offline, fake session."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.models.database import LeadEvent, OpenHouseFormRequest  # noqa: E402
from app.services import tasks_service  # noqa: E402


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    async def get(self, model, key):
        return self.rows.get((model, key))


def _setup(rule):
    ev = SimpleNamespace(id="ev1", source_id="req1", agent_viewed_at=None)
    req = SimpleNamespace(id="req1", completed_at=None)
    db = FakeDb({(LeadEvent, "ev1"): ev, (OpenHouseFormRequest, "req1"): req})
    task = SimpleNamespace(auto_source=rule, auto_source_id="ev1")
    return db, task, ev, req


def test_disclosure_followup_done_marks_request_sent():
    db, task, ev, req = _setup("disclosure_request")
    assert asyncio.run(tasks_service.resolve_request_for_followup(db, task)) is True
    assert req.completed_at is not None and ev.agent_viewed_at is not None


def test_tour_and_second_visit_followups_need_a_real_answer():
    for rule in ("showing_request", "second_visit_request"):
        db, task, ev, req = _setup(rule)
        assert asyncio.run(tasks_service.resolve_request_for_followup(db, task)) is False
        assert req.completed_at is None
