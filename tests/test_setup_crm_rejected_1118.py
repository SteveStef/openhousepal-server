"""11/18 setup deep audit: a Follow Up Boss key that FUB rejected must not count as a finished
CRM step (setup used to show "Connected" + Done while no lead could get through)."""
from types import SimpleNamespace

from app.services.setup_flow_service import crm_step_done

REJECTED = ("⚠ Follow Up Boss rejected this API key. Double-check you copied the whole key from FUB → "
            "Admin → API → Create API Key (it's shown only once). Leads won't reach Follow Up Boss until the key is valid.")
TEAM_NOTE = "This key belongs to a regular user on the Follow Up Boss account “Team”. New leads ..."


def agent(**kw):
    base = dict(crm_push_enabled=True, crm_fub_api_key=None, crm_fub_account_note=None, crm_webhook_url=None, crm_parse_email=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_good_fub_key_counts():
    assert crm_step_done(agent(crm_fub_api_key="fka_good"))


def test_team_key_heads_up_still_counts():
    assert crm_step_done(agent(crm_fub_api_key="fka_team", crm_fub_account_note=TEAM_NOTE))


def test_rejected_fub_key_does_not_count():
    assert not crm_step_done(agent(crm_fub_api_key="fka_bad", crm_fub_account_note=REJECTED))


def test_rejected_key_but_working_email_counts():
    assert crm_step_done(agent(crm_fub_api_key="fka_bad", crm_fub_account_note=REJECTED, crm_parse_email="leads@crm.example.com"))


def test_switched_off_never_counts():
    assert not crm_step_done(agent(crm_push_enabled=False, crm_fub_api_key="fka_good"))
    assert not crm_step_done(agent())
