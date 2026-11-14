"""Owner, 11/13: "re-running a CMA should give the same number". 701 Sturbridge Dr went
$1.80M -> $1.75M -> $1.725M with no edits: the re-run's estimate pass had no reuse at all and the
prompt-keyed caches missed on a corrected comp note / late road data. Each report now keeps its own
AI answers, keyed on its FACTS only (cma_ai_service._value_lock_identity)."""
import copy
import sys
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_ai_service as svc  # noqa: E402

SUBJECT = {"street_address": "701 Sturbridge Dr", "city": "Bryn Mawr", "state": "PA", "list_price": 1875000,
           "home_status": "ACTIVE-BRIGHT", "beds": 5, "baths": 4.5, "sqft": 5200, "year_built": 1998,
           "public_remarks": "Stately colonial with a pool."}
COMPS = [{"listing_key": "b", "sale_price": 1700000, "list_price": 1650000, "close_date": "2026-08-01", "beds": 5, "baths": 4,
          "sqft": 5000, "year_built": 1995, "quality": {"finish": "similar"}, "commentary": "old note"},
         {"listing_key": "a", "sale_price": 1810000, "list_price": 1795000, "close_date": "2026-09-12", "beds": 5, "baths": 5,
          "sqft": 5400, "year_built": 2001, "quality": {"finish": "superior"}, "commentary": "old note"}]


def _fp(subject, comps, ratings=True):
    return svc._value_lock_fingerprint(svc._value_lock_identity(subject, comps, with_ratings=ratings), model="m", mode="buyer")


def test_notes_road_data_days_and_comp_order_are_not_a_change():
    comps = copy.deepcopy(COMPS)
    comps[0]["commentary"] = comps[0]["ai_commentary"] = "a corrected note"
    comps[1]["road_context"] = {"verdict": "through"}
    comps[1]["days_on_market"] = 44
    subject = {**SUBJECT, "road_context_note": "fronts Sproul Road", "days_on_market": 56}
    assert _fp(subject, list(reversed(comps))) == _fp(SUBJECT, COMPS)


@pytest.mark.parametrize("change", [
    lambda s, c: s.update(list_price=1825000),                  # price drop
    lambda s, c: s.update(home_status="PENDING-BRIGHT"),        # status change
    lambda s, c: s.update(sqft=5600),                           # agent corrected the size
    lambda s, c: c[0].update(sale_price=1690000),               # comp's sale price corrected
    lambda s, c: c.pop(),                                       # comp removed
    lambda s, c: c[1].update(is_included=False),                # comp switched off
    lambda s, c: c[0]["quality"].update(finish="inferior"),     # agent changed a rating
])
def test_a_real_change_gets_a_fresh_answer(change):
    s, c = copy.deepcopy(SUBJECT), copy.deepcopy(COMPS)
    change(s, c)
    assert _fp(s, c) != _fp(SUBJECT, COMPS)


def test_the_comp_pass_identity_ignores_ratings_it_is_about_to_write():
    c = copy.deepcopy(COMPS)
    c[0]["quality"]["finish"] = "inferior"
    assert _fp(SUBJECT, c, ratings=False) == _fp(SUBJECT, COMPS, ratings=False)


def _packet(note="note", weight=0.5, anchor=1750000, price=1700000):
    return {"report_mode": "buyer",
            "subject": {"address": "701 Sturbridge Dr, Bryn Mawr, PA", "current_list_price": 1875000, "status": "Active",
                        "dom": 55, "beds": 5, "baths": 4.5, "above_grade_sqft": 5200, "listing_remarks": "Stately."},
            "comparables": [{"comp_id": "a", "sale_price": price, "list_price": 1650000, "sale_date": "2026-08-01", "dom": 9,
                             "above_grade_sqft": 5000, "comp_narrative": note, "normalized_weight": weight,
                             "street_context": {"verdict": "quiet"}, "better_worse_ratings": {"finish": "similar"}}],
            "deterministic_evidence": {"deterministic_anchor": anchor}}


def test_packet_identity_ignores_notes_weights_and_the_days_drift_of_the_anchor():
    f = lambda p: svc._value_lock_fingerprint(svc._packet_lock_identity(p), model="m")
    assert f(_packet()) == f(_packet(note="a corrected note", weight=0.47, anchor=1752600))
    assert f(_packet()) != f(_packet(price=1690000))


@pytest.mark.asyncio
async def test_a_rerun_with_only_a_note_changed_reuses_the_reports_answer(monkeypatch):
    store, calls = {}, []

    async def cache_get(db, key):
        return copy.deepcopy(store.get(key))

    async def cache_put(db, key, raw):
        store[key] = copy.deepcopy(raw)

    async def call_model(config, packet):
        calls.append(1)
        return {"final_fmv": 1800000 - 25000 * (len(calls) - 1), "range_low": 1700000, "range_high": 1900000}, {"input_tokens": 1, "output_tokens": 1}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(svc, "_reasoning_cache_get", cache_get)
    monkeypatch.setattr(svc, "_reasoning_cache_put", cache_put)
    monkeypatch.setattr(svc, "_call_cma_reasoning_model", call_model)
    monkeypatch.setattr(svc, "_validate_reasoning_valuation", lambda raw, packet: (copy.deepcopy(raw), []))
    monkeypatch.setattr(svc, "_reasoning_projected_cost", lambda config, packet: (0.0, 1.0))
    monkeypatch.setattr(svc, "reconcile_budget", noop)
    monkeypatch.setattr(svc, "record_usage_event", noop)
    monkeypatch.setattr(svc.ai_config_service, "estimate_cost_usd", lambda *a, **k: 0.0)
    config = {"cma_reasoning_model": "m"}

    first, _, _, _ = await svc._run_cma_reasoning_stage(None, "o", "IDX", config, _packet(), "report-1", reservation=object())
    # the re-run: a comp note was corrected, a weight drifted, the anchor moved a few dollars
    second, _, _, usage = await svc._run_cma_reasoning_stage(
        None, "o", "IDX", config, _packet(note="a corrected note", weight=0.47, anchor=1752600), "report-1", reservation=object())
    assert len(calls) == 1 and usage == {"cached": True}
    assert second["final_fmv"] == first["final_fmv"] == 1800000

    # a real change (the comp's sale price was corrected) asks again
    third, _, _, _ = await svc._run_cma_reasoning_stage(
        None, "o", "IDX", config, _packet(price=1690000), "report-1", reservation=object())
    assert len(calls) == 2 and third["final_fmv"] == 1775000

    # another report with the same facts and a different note doesn't borrow report-1's answer
    await svc._run_cma_reasoning_stage(None, "o", "IDX", config, _packet(note="other"), "report-2", reservation=object())
    assert len(calls) == 3
