"""Central PayPal plan-ID <-> PlanTier lookup, shared by subscription_routes.py (upgrade/downgrade/
new-signup) and webhook_routes.py (activation/renewal sync) so the two never drift apart.

Basic and Premium each have two plan-ID generations: the legacy pair (grandfathered existing
subscribers, original $9.95/$24.95 pricing, never edited) and the V2 pair (current $24.95/$49.95
pricing, used for every new signup and every plan change). No-MLS and IDX only ever had one
pricing generation. See project pricing-restructure plan for the full rationale.
"""
import os

_PLAN_ID_ENV_PAIRS_BY_TIER = {
    "NO_MLS": [("PAYPAL_NO_MLS_PLAN_ID", "PAYPAL_NO_MLS_NO_TRIAL_PLAN_ID")],
    "PRO_NO_MLS": [("PAYPAL_PRO_NO_MLS_PLAN_ID", "PAYPAL_PRO_NO_MLS_NO_TRIAL_PLAN_ID")],
    "BASIC": [
        ("PAYPAL_BASIC_PLAN_ID", "PAYPAL_BASIC_NO_TRIAL_PLAN_ID"),           # legacy $9.95 - grandfathered only
        ("PAYPAL_BASIC_V2_PLAN_ID", "PAYPAL_BASIC_V2_NO_TRIAL_PLAN_ID"),     # current $24.95
    ],
    "PREMIUM": [
        ("PAYPAL_PREMIUM_PLAN_ID", "PAYPAL_PREMIUM_NO_TRIAL_PLAN_ID"),       # legacy $24.95 - grandfathered only
        ("PAYPAL_PREMIUM_V2_PLAN_ID", "PAYPAL_PREMIUM_V2_NO_TRIAL_PLAN_ID"), # current $49.95
    ],
    "IDX": [("PAYPAL_IDX_PLAN_ID", "PAYPAL_IDX_NO_TRIAL_PLAN_ID")],
}

# Plan IDs to use for a NEW plan change (signup, upgrade, downgrade) - always the current-price,
# no-trial variant. Never the legacy IDs: grandfathering protects staying on your CURRENT tier
# only, not future upgrades - a legacy Basic subscriber who upgrades pays current Premium
# pricing, never a blended legacy rate (explicit product decision).
CURRENT_NO_TRIAL_PLAN_ID_BY_TIER = {
    "NO_MLS": os.getenv("PAYPAL_NO_MLS_NO_TRIAL_PLAN_ID"),
    "PRO_NO_MLS": os.getenv("PAYPAL_PRO_NO_MLS_NO_TRIAL_PLAN_ID"),
    "BASIC": os.getenv("PAYPAL_BASIC_V2_NO_TRIAL_PLAN_ID"),
    "PREMIUM": os.getenv("PAYPAL_PREMIUM_V2_NO_TRIAL_PLAN_ID"),
    "IDX": os.getenv("PAYPAL_IDX_NO_TRIAL_PLAN_ID"),
}

# Human-facing tier names. `.title()` on the raw enum value turns "PRO_NO_MLS" into the ugly
# "Pro_No_Mls", so anywhere a tier name is shown to a user, prefer display_name_for_tier().
PLAN_DISPLAY_NAMES = {
    "NO_MLS": "No-MLS Basic",
    "PRO_NO_MLS": "No-MLS Premium",
    "BASIC": "Basic",
    "PREMIUM": "Premium",
    "IDX": "IDX",
}


def display_name_for_tier(tier):
    """User-facing name for a PlanTier string value; falls back to a tidied version of the
    raw value for anything unrecognized."""
    if not tier:
        return ""
    return PLAN_DISPLAY_NAMES.get(tier, str(tier).replace("_", " ").title())


def _build_lookup():
    plan_id_to_tier = {}
    trial_plan_ids = set()
    for tier, pairs in _PLAN_ID_ENV_PAIRS_BY_TIER.items():
        for trial_env, no_trial_env in pairs:
            trial_id = os.getenv(trial_env)
            no_trial_id = os.getenv(no_trial_env)
            if trial_id:
                plan_id_to_tier[trial_id] = tier
                # IDX never has a free trial (its sign-up plan carries the setup fee instead), so its
                # sign-up plan must never put an account on TRIAL.
                if tier != "IDX":
                    trial_plan_ids.add(trial_id)
            if no_trial_id:
                plan_id_to_tier[no_trial_id] = tier
    return plan_id_to_tier, trial_plan_ids


PLAN_ID_TO_TIER, TRIAL_PLAN_IDS = _build_lookup()

# The plans checkout offers a brand-new agent (the first ID of each pair, incl. IDX's sign-up plan).
# The other IDs are for plan changes and resubscribes, and only reachable at sign-up through a code.
SIGNUP_PLAN_IDS = {os.getenv(trial_env) for pairs in _PLAN_ID_ENV_PAIRS_BY_TIER.values() for trial_env, _ in pairs} - {None, ""}


def tier_for_plan_id(plan_id):
    """Resolve any recognized PayPal plan ID (legacy or current, any tier) to its PlanTier
    string value. Returns None for an unrecognized plan ID."""
    return PLAN_ID_TO_TIER.get(plan_id)


def plan_id_has_trial(plan_id):
    """True only for the trial-bearing variant of a plan ID (not its *_NO_TRIAL sibling)."""
    return plan_id in TRIAL_PLAN_IDS


NO_MLS_TIERS = {"NO_MLS", "PRO_NO_MLS"}
NOT_IN_BRIGHT_MESSAGE = ("Your account isn't connected to Bright MLS, so it can use the No-MLS plans "
                         "(No-MLS Basic or No-MLS Premium).")


def tier_allowed_for(user, tier) -> bool:
    """Bright plans need a Bright MLS ID; agents outside Bright can only use the No-MLS plans."""
    return bool(getattr(user, "mls_id", None)) or tier in NO_MLS_TIERS


def listing_owner_key(user):
    """What ties a listing to its agent in Property.list_agent_mls_id. Bright agents: their MLS ID.
    Agents outside Bright (no MLS ID) own hand-entered listings via "user:<id>" - a value no real
    Bright MLS ID can ever equal, so their listings never mix with someone else's."""
    mls_id = (getattr(user, "mls_id", None) or "").strip()
    return mls_id or f"user:{user.id}"
