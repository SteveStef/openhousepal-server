import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.email_avatar_service import agent_email_vars
from app.database import get_db
from app.models.database import AgentForm, AgentHub, AgentHubEvent, FormCampaign, FormSubmission, FormViewSession, Notification, User
from app.schemas.forms import (
    AgentFormCreate,
    AgentFormLayoutUpdate,
    AgentFormResponse,
    AgentFormUpdate,
    CampaignLookupResponse,
    ExampleProperty,
    FormCampaignCreate, FormCampaignUpdate,
    FormCampaignResponse,
    FormProgressUpdate,
    FormSubmissionCreate,
    FormSubmissionListItem,
    FormSubmissionResponse,
    PublicAgentFormResponse,
    PublicFormSummary,
    SlugCheckResponse,
    SlugUpdate,
    SubmissionReviewedUpdate,
)
from app.utils.auth import get_current_active_user, get_current_user_optional, require_active_plan
from app.services.forms_service import record_form_progress, normalize_campaign_redirect, campaign_after_submit, active_campaign_for_token
from app.services.leads_service import upsert_lead
from app.services.agent_email_service import agent_public_phone

router = APIRouter(prefix="/agent-hub/forms", tags=["forms"])
logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_FORM_SLUGS = {"by-type", "sales"}
BRAND_ACCENT = "#816937"  # RGB(129,105,55) - default form accent color
PROPERTY_LINK_DISABLED_TYPES = {"buying", "selling", "home_value", "seller_onboarding", "buyer_onboarding", "how_can_i_help", "showcase_request"}  # only true custom/manual forms support property-linking

US_STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]

def showcase_criteria_questions(gate_id: str, gate_values: List[str]) -> List[dict]:
    """The exact home-search questions from Setup My Own Showcase (same labels, types and answer
    ids, so CollectionsService._preferences_from_showcase_form_answers reads them unchanged),
    shown only when `gate_id` is answered with one of `gate_values` (i.e. not already working with
    an agent). Locked: the Showcase these forms build depends on them. Group ids are prefixed so
    they can't collide with a form's own questions (e.g. I'm Buying's "price_range")."""
    dep = {"question_id": gate_id, "values": list(gate_values)}
    return [
        {"id": "sc_area", "type": "contact_group", "label": "Where are you looking?", "required": False, "locked": True,
         "depends_on": dep, "section_title": "Your Home Search",
         "fields": [
             {"id": "cities", "type": "city_multiselect", "label": "Cities / Towns", "required": False},
             {"id": "school_districts", "type": "school_district_multiselect", "label": "School Districts", "required": False},
             {"id": "county", "type": "county_multiselect", "label": "County", "required": False},
             {"id": "state", "type": "select", "label": "State", "required": False, "searchable": True, "options": US_STATE_NAMES},
         ]},
        {"id": "sc_price_range", "type": "contact_group", "label": "Price Range", "required": False, "locked": True, "depends_on": dep,
         "fields": [
             {"id": "budget_min", "type": "currency", "label": "Minimum price", "required": False},
             {"id": "budget_max", "type": "currency", "label": "Maximum price", "required": False},
         ]},
        {"id": "sc_beds", "type": "contact_group", "label": "How many bedrooms do you need?", "required": False, "locked": True, "depends_on": dep,
         "fields": [
             {"id": "min_bedrooms", "type": "number", "label": "Minimum bedrooms", "required": False},
         ]},
        {"id": "sc_acres", "type": "contact_group", "label": "Lot size (acres)", "required": False, "locked": True, "depends_on": dep,
         "fields": [
             {"id": "min_acres", "type": "number", "label": "Minimum acres", "required": False},
             {"id": "max_acres", "type": "number", "label": "Maximum acres", "required": False},
         ]},
        {"id": "sc_sqft", "type": "contact_group", "label": "Living area (sq ft)", "required": False, "locked": True, "depends_on": dep,
         "fields": [
             {"id": "min_sqft", "type": "number", "label": "Minimum sq ft", "required": False},
             {"id": "max_sqft", "type": "number", "label": "Maximum sq ft", "required": False},
         ]},
        {"id": "sc_build_year", "type": "contact_group", "label": "Build year", "required": False, "locked": True, "depends_on": dep,
         "fields": [
             {"id": "min_year_built", "type": "number", "label": "Built in or after", "required": False},
             {"id": "max_year_built", "type": "number", "label": "Built in or before", "required": False},
         ]},
        {"id": "home_type", "type": "multi_select", "label": "What type of property are you looking for?", "required": False, "locked": True,
         "depends_on": dep, "options": ["Single-family home", "Condo/Townhouse", "Multi-family", "Land", "Other"]},
        {"id": "key_features", "type": "multi_select", "label": "What key features would you like?", "required": False, "locked": True,
         "depends_on": dep,
         "options": ["Pool", "Cul-de-sac", "First floor primary suite", "Garage", "Fenced yard", "Open floor plan", "Updated kitchen", "Waterfront", "Finished basement", "New construction"]},
    ]


# I'm Buying has no "working with an agent?" question of its own - it gets Setup My Own Showcase's
# (without the dead-end: someone with an agent just skips the home-search questions).
BUYING_AGENT_GATE = {
    "id": "sc_has_agent", "type": "select", "label": "Do you already have an agent helping you buy?", "required": True, "locked": True,
    "options": ["No, I'm not working with an agent", "Yes, I already have an agent"],
}
HOW_CAN_I_HELP_GATE_VALUES = ["No", "Not sure"]


DEFAULT_FORMS = {
    "buying": {
        "slug": "buying",
        "title": "I'm Buying",
        "description": "Quickly collect the basic information needed from a new buyer lead.",
        "headline": "Let's Find Your Next Home",
        "subtitle": "Tell me a bit about what you're looking for and I'll follow up with a personalized plan.",
        "success_heading": "You're all set!",
        "success_message": "Thanks for sharing those details — I'll be in touch soon with homes that fit.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone", "required": True},
             ]},
            {"id": "areas", "type": "short_text", "label": "What towns, neighborhoods, schools are you interested in?", "required": False, "locked": False},
            {"id": "price_range", "type": "short_text", "label": "What's your target price range?", "required": False, "locked": False},
            {"id": "timeline", "type": "select", "label": "When are you hoping to buy?", "required": False, "locked": False,
             "options": ["Right away", "1–3 months", "3–6 months", "6–12 months", "Just researching"]},
            {"id": "pre_approved", "type": "select", "label": "Are you pre-approved for a mortgage?", "required": False, "locked": False,
             "options": ["Yes", "No", "Working on it", "No, I'm paying cash"]},
            {"id": "notes", "type": "long_text", "label": "Anything else we should know?", "required": False, "locked": False},
        ],
    },
    "selling": {
        "slug": "selling",
        "title": "I'm Selling",
        "description": "Quickly collect the basic information needed from a potential seller lead.",
        "headline": "Let's Get Your Home Sold",
        "subtitle": "A few quick questions so I can put together a plan to get your home sold.",
        "success_heading": "Got it — thank you!",
        "success_message": "I'll review your details and reach out shortly to talk next steps.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone", "required": True},
             ]},
            {"id": "property_address", "type": "address_autocomplete", "label": "Property address", "required": True, "locked": True},
            {"id": "price_expectation", "type": "short_text", "label": "What price are you looking for?", "required": False, "locked": False},
            {"id": "move_reason", "type": "short_text", "label": "What's prompting the move?", "required": False, "locked": False},
            {"id": "timeline", "type": "select", "label": "When are you hoping to sell?", "required": False, "locked": False,
             "options": ["Right away", "1–3 months", "3–6 months", "6–12+ months", "Flexible"]},
            {"id": "notes", "type": "long_text", "label": "Anything else?", "required": False, "locked": False},
        ],
    },
    "home_value": {
        "slug": "home-value",
        "title": "What's My Home Worth?",
        "description": "Collect a potential sellers information to create a home-value estimate.",
        "headline": "Let's Find Out What Your Home Is Worth",
        "subtitle": "Get a quick market valuation for your property.",
        "success_heading": "Thanks!",
        "success_message": "I'll put together your home value estimate and follow up soon.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "property_address", "type": "address_autocomplete", "label": "Property address", "required": True, "locked": True},
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone number", "required": True},
             ]},
            {"id": "estimated_value", "type": "short_text", "label": "What do you think your home is worth?", "required": False, "locked": False},
            {"id": "square_footage", "type": "number", "label": "Approximate square footage", "required": False, "locked": False},
            {"id": "bedrooms", "type": "number", "label": "Bedrooms", "required": False, "locked": False},
            {"id": "bathrooms", "type": "number", "label": "Bathrooms", "required": False, "locked": False},
            {"id": "upgrades", "type": "long_text", "label": "What upgrades have you done since buying?", "required": False, "locked": False},
            {"id": "sell_timeline", "type": "select", "label": "Are you thinking of selling?", "required": False, "locked": False,
             "options": ["Yes, right away", "1–3 months", "3–6 months", "6–12+ months", "Not interested", "Just curious of current value"]},
            {"id": "notes", "type": "long_text", "label": "Anything else about the property?", "required": False, "locked": False},
        ],
    },
    "showing": {
        "slug": "schedule-a-showing",
        "title": "Schedule a Showing",
        "description": "A quick form for someone requesting to tour a specific property.",
        "headline": "Schedule a Showing",
        "subtitle": "Quick details so I can set up a time to show you this home.",
        "success_heading": "You're all set!",
        "success_message": "Thanks! I'll confirm your showing time shortly.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "property_of_interest", "type": "short_text", "label": "What property are you interested in seeing?", "required": True, "locked": True},
            {"id": "preferred_times", "type": "contact_group", "label": "What 2 dates and times would work for you?", "required": True, "locked": True,
             "fields": [
                 {"id": "preferred_date_1", "type": "date", "label": "First choice - date", "required": True},
                 {"id": "preferred_time_1", "type": "time", "label": "First choice - time", "required": True},
                 {"id": "preferred_date_2", "type": "date", "label": "Second choice - date", "required": False},
                 {"id": "preferred_time_2", "type": "time", "label": "Second choice - time", "required": False},
             ]},
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Name", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone number", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
             ]},
        ],
    },
    "how_can_i_help": {
        "slug": "how-can-i-help",
        "title": "How Can We Help",
        "description": "A general lead form for all leads - typically used in bios, YouTube descriptions, etc.",
        "headline": "How Can We Help",
        "subtitle": "Tell me a bit about what you need and I'll follow up with the right next steps.",
        "success_heading": "Thanks for reaching out!",
        "success_message": "I'll review what you shared and be in touch soon.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "intent", "type": "select", "label": "How can I help you?", "required": True, "locked": True,
             "options": ["I'm looking to buy", "I'm looking to sell"]},

            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone", "required": True},
             ]},

            # Buyer path - each select gets its own screen (contact_group sub-fields don't support
            # option pickers, only free-typed values, so selects can't be bundled into a group).
            {"id": "areas", "type": "short_text", "label": "What towns, neighborhoods, schools are you interested in?", "required": False, "locked": False,
             "section_title": "Buying", "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},
            {"id": "price_range", "type": "short_text", "label": "What's your target price range?", "required": False, "locked": False,
             "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},
            {"id": "buy_property_type", "type": "select", "label": "What type of property are you looking for?", "required": False, "locked": False,
             "options": ["Single-family home", "Condo/Townhouse", "Multi-family", "Land", "Other"],
             "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},
            {"id": "buy_beds_baths", "type": "contact_group", "label": "How many bedrooms and bathrooms do you need?", "required": False, "locked": False,
             "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]},
             "fields": [
                 {"id": "buy_bedrooms", "type": "number", "label": "Bedrooms", "required": False},
                 {"id": "buy_bathrooms", "type": "number", "label": "Bathrooms", "required": False},
             ]},
            {"id": "buy_timeline", "type": "select", "label": "When are you hoping to buy?", "required": False, "locked": False,
             "options": ["ASAP", "1–3 months", "3–6 months", "6–12 months", "Just researching"],
             "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},
            {"id": "buy_working_with_agent", "type": "select", "label": "Are you currently working with another real estate agent?", "required": False, "locked": False,
             "options": ["Yes", "No", "Not sure"], "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},
            {"id": "pre_approved", "type": "select", "label": "Are you pre-approved for a mortgage?", "required": False, "locked": False,
             "options": ["Yes", "No", "Working on it", "No, I'm paying cash"],
             "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},
            {"id": "buy_notes", "type": "long_text", "label": "Anything else we should know?", "required": False, "locked": False,
             "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]}},

            # Seller path
            {"id": "property_address", "type": "short_text", "label": "Property address", "required": True, "locked": True,
             "section_title": "Selling", "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
            {"id": "price_expectation", "type": "short_text", "label": "What price are you hoping to get?", "required": False, "locked": False,
             "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
            {"id": "sell_property_type", "type": "select", "label": "What type of property is it?", "required": False, "locked": False,
             "options": ["Single-family home", "Condo/Townhouse", "Multi-family", "Land", "Other"],
             "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
            {"id": "sell_beds_baths_size", "type": "contact_group", "label": "Property details", "required": False, "locked": False,
             "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]},
             "fields": [
                 {"id": "sell_bedrooms", "type": "number", "label": "Bedrooms", "required": False},
                 {"id": "sell_bathrooms", "type": "number", "label": "Bathrooms", "required": False},
                 {"id": "sell_square_footage", "type": "number", "label": "Approximate square footage", "required": False},
             ]},
            {"id": "sell_timeline", "type": "select", "label": "When are you hoping to sell?", "required": False, "locked": False,
             "options": ["ASAP", "1–3 months", "3–6 months", "6–12+ months", "Just curious"],
             "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
            {"id": "currently_listed", "type": "select", "label": "Is the property currently listed?", "required": False, "locked": False,
             "options": ["Yes", "No"], "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
            {"id": "sell_working_with_agent", "type": "select", "label": "Are you currently working with another agent?", "required": False, "locked": False,
             "options": ["Yes", "No", "Not sure"], "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
            {"id": "sell_notes", "type": "long_text", "label": "Anything else we should know?", "required": False, "locked": False,
             "depends_on": {"question_id": "intent", "values": ["I'm looking to sell"]}},
        ],
    },
    "seller_onboarding": {
        "slug": "seller-onboarding",
        "title": "Seller Onboarding",
        "description": "A fuller intake once a seller becomes a client - what you need to prep, market, and list.",
        "headline": "Let's Get Your Listing Started",
        "subtitle": "A few details to get your listing set up correctly from day one.",
        "success_heading": "All set!",
        "success_message": "Thanks for the details — I'll follow up if I have any questions before we move forward.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "seller_count", "type": "select", "label": "How many owners will be listed on the property sale?", "required": True, "locked": False,
             "section_title": "Seller Information", "options": ["1 Seller", "2 Sellers"]},

            {"id": "seller_contact", "type": "contact_group", "label": "Seller Information", "required": True, "locked": True,
             "depends_on": {"question_id": "seller_count", "values": ["1 Seller"]},
             "fields": [
                 {"id": "seller_name", "type": "short_text", "label": "Legal Name", "required": True},
                 {"id": "seller_phone", "type": "phone", "label": "Phone Number", "required": True},
                 {"id": "seller_email", "type": "email", "label": "Email", "required": True},
                 {"id": "seller_birthdate", "type": "date", "label": "Birthdate", "required": True},
             ]},

            {"id": "seller1_contact", "type": "contact_group", "label": "Contact Information", "required": True, "locked": True,
             "section_title": "Seller 1", "depends_on": {"question_id": "seller_count", "values": ["2 Sellers"]},
             "fields": [
                 {"id": "seller1_name", "type": "short_text", "label": "Legal Name", "required": True},
                 {"id": "seller1_phone", "type": "phone", "label": "Phone Number", "required": True},
                 {"id": "seller1_email", "type": "email", "label": "Email", "required": True},
                 {"id": "seller1_birthdate", "type": "date", "label": "Birthdate", "required": True},
             ]},
            {"id": "seller2_contact", "type": "contact_group", "label": "Contact Information", "required": True, "locked": True,
             "section_title": "Seller 2", "depends_on": {"question_id": "seller_count", "values": ["2 Sellers"]},
             "fields": [
                 {"id": "seller2_name", "type": "short_text", "label": "Legal Name", "required": True},
                 {"id": "seller2_phone", "type": "phone", "label": "Phone Number", "required": True},
                 {"id": "seller2_email", "type": "email", "label": "Email", "required": True},
                 {"id": "seller2_birthdate", "type": "date", "label": "Birthdate", "required": True},
             ]},

            {"id": "property_address_group", "type": "contact_group", "label": "Property Address", "required": True, "locked": True,
             "fields": [
                 {"id": "address_line1", "type": "short_text", "label": "Address", "required": True},
                 {"id": "address_line2", "type": "short_text", "label": "Address Line 2", "required": False},
                 {"id": "city", "type": "short_text", "label": "City/Town", "required": True},
                 {"id": "state", "type": "short_text", "label": "State/Region/Province", "required": True},
                 {"id": "zip", "type": "short_text", "label": "ZIP/Postal Code", "required": True},
                 {"id": "country", "type": "short_text", "label": "Country", "required": False},
             ]},

            {"id": "additional_owner", "type": "select", "label": "Is there anyone else on the deed or mortgage?", "required": True, "locked": False,
             "section_title": "Additional Ownership", "options": ["Yes", "No", "Not sure"]},
            {"id": "additional_owner_details", "type": "long_text", "label": "Please provide their name and explain whether they are on the deed, mortgage, or both.", "required": True, "locked": False,
             "depends_on": {"question_id": "additional_owner", "values": ["Yes"]}},

            {"id": "listing_timeline", "type": "select", "label": "When are you hoping to list the property?", "required": True, "locked": False,
             "section_title": "Listing Timeline",
             "options": ["As soon as possible", "Within 30 days", "Within 1–3 months", "Within 3–6 months", "More than 6 months from now", "I'm not sure yet", "Specific date"]},
            {"id": "listing_specific_date", "type": "date", "label": "What date are you hoping to list?", "required": True, "locked": False,
             "depends_on": {"question_id": "listing_timeline", "values": ["Specific date"]}},

            {"id": "square_footage", "type": "number", "label": "What is the square footage of your property? An estimate is fine.", "required": False, "locked": False,
             "section_title": "Property Details", "unit": "square feet"},
            {"id": "bedrooms", "type": "number", "label": "How many bedrooms does your property have?", "required": False, "locked": False},
            {"id": "full_bathrooms", "type": "number", "label": "How many full bathrooms does your property have?", "required": False, "locked": False},
            {"id": "partial_bathrooms", "type": "number", "label": "How many partial bathrooms does your property have?", "required": False, "locked": False,
             "helper_text": "Includes half bathrooms or other bathrooms without a full tub or shower."},
            {"id": "central_air", "type": "select", "label": "Does your property have central air conditioning?", "required": False, "locked": False,
             "options": ["Yes", "No", "Partially", "Not sure"]},
            {"id": "parking", "type": "multi_select", "label": "What kind of parking does your property have? Select all that apply.", "required": False, "locked": False,
             "options": ["Attached garage", "Detached garage", "Driveway", "Assigned parking", "Street parking", "Parking lot", "Carport", "No dedicated parking", "Other"]},
            {"id": "parking_other", "type": "short_text", "label": "Please describe your parking", "required": False, "locked": False,
             "depends_on": {"question_id": "parking", "values": ["Other"]}},

            {"id": "special_features", "type": "long_text", "label": "Does your property include any other special features, renovations, or upgrades?", "required": False, "locked": False,
             "section_title": "Property Features"},
            {"id": "love_about_home", "type": "long_text", "label": "What do you love about your home, and what makes it unique?", "required": False, "locked": False},
            {"id": "value_reducers", "type": "long_text", "label": "Are there any features or conditions of the property that you feel may reduce its value?", "required": False, "locked": False},

            {"id": "desired_price", "type": "currency", "label": "What is your view of the value of your property? How much would you like to list it for?", "required": False, "locked": False,
             "section_title": "Seller's View of Value"},
            {"id": "desired_price_explanation", "type": "long_text", "label": "Why do you feel that's the right price?", "required": False, "locked": False},

            {"id": "has_mortgage", "type": "select", "label": "Do you have a mortgage on this property?", "required": False, "locked": False,
             "section_title": "Mortgage Information", "options": ["Yes", "No", "Not sure"]},
            {"id": "mortgage_owed", "type": "currency", "label": "Approximately how much do you currently owe on the property? Please include all mortgages, second mortgages, home-equity loans, and lines of credit.", "required": False, "locked": False,
             "helper_text": "An estimate is fine.", "depends_on": {"question_id": "has_mortgage", "values": ["Yes"]}},

            {"id": "consultation_notes", "type": "long_text", "label": "Do you have any questions or concerns that you would like us to address during your listing consultation appointment?", "required": False, "locked": False,
             "section_title": "Listing Consultation"},
        ],
    },
    "buyer_onboarding": {
        "slug": "buyer-onboarding",
        "title": "Buyer Onboarding",
        "description": "A fuller intake once a buyer becomes a client - what you need to start representing them.",
        "headline": "Let's Get You Set Up",
        "subtitle": "A few details so I can start finding the right homes for you right away.",
        "success_heading": "All set!",
        "success_message": "Thanks for the details — I'll be in touch as we start the search.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "buyer_count", "type": "select", "label": "How many people will be involved in purchasing your next property?", "required": True, "locked": False,
             "section_title": "Buyer Information", "helper_text": "A married couple typically counts as two people.",
             "options": ["1 Buyer", "2 Buyers"]},

            {"id": "buyer_contact", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "depends_on": {"question_id": "buyer_count", "values": ["1 Buyer"]},
             "fields": [
                 {"id": "buyer_first_name", "type": "short_text", "label": "First Name", "required": True},
                 {"id": "buyer_last_name", "type": "short_text", "label": "Last Name", "required": True},
                 {"id": "buyer_phone", "type": "phone", "label": "Phone Number", "required": True},
                 {"id": "buyer_email", "type": "email", "label": "Email", "required": True},
                 {"id": "buyer_dob", "type": "date", "label": "Date of Birth", "required": True},
             ]},

            {"id": "buyer1_contact", "type": "contact_group", "label": "Contact Information", "required": True, "locked": True,
             "section_title": "Buyer 1", "depends_on": {"question_id": "buyer_count", "values": ["2 Buyers"]},
             "fields": [
                 {"id": "buyer1_first_name", "type": "short_text", "label": "First Name", "required": True},
                 {"id": "buyer1_last_name", "type": "short_text", "label": "Last Name", "required": True},
                 {"id": "buyer1_phone", "type": "phone", "label": "Phone Number", "required": True},
                 {"id": "buyer1_email", "type": "email", "label": "Email", "required": True},
                 {"id": "buyer1_dob", "type": "date", "label": "Date of Birth", "required": True},
             ]},

            {"id": "buyer2_contact", "type": "contact_group", "label": "Contact Information", "required": True, "locked": True,
             "section_title": "Buyer 2", "depends_on": {"question_id": "buyer_count", "values": ["2 Buyers"]},
             "fields": [
                 {"id": "buyer2_first_name", "type": "short_text", "label": "First Name", "required": True},
                 {"id": "buyer2_last_name", "type": "short_text", "label": "Last Name", "required": True},
                 {"id": "buyer2_phone", "type": "phone", "label": "Phone Number", "required": True},
                 {"id": "buyer2_email", "type": "email", "label": "Email", "required": True},
                 {"id": "buyer2_dob", "type": "date", "label": "Date of Birth", "required": True},
             ]},

            {"id": "mailing_address_group", "type": "contact_group", "label": "Current Mailing Address", "required": True, "locked": True,
             "fields": [
                 {"id": "mailing_address_line1", "type": "short_text", "label": "Address", "required": True},
                 {"id": "mailing_address_line2", "type": "short_text", "label": "Address Line 2", "required": False},
                 {"id": "mailing_city", "type": "short_text", "label": "City/Town", "required": True},
                 {"id": "mailing_state", "type": "short_text", "label": "State/Region/Province", "required": True},
                 {"id": "mailing_zip", "type": "short_text", "label": "ZIP/Postal Code", "required": True},
                 {"id": "mailing_country", "type": "short_text", "label": "Country", "required": False},
             ]},

            {"id": "decision_makers", "type": "select", "label": "Will you be the sole purchaser or purchasers, or is anyone else involved in the financial or decision-making process?", "required": True, "locked": False,
             "section_title": "Additional Decision-Makers",
             "options": ["We will be the only purchasers and decision-makers", "Someone else is financially involved", "Someone else is involved in the decision-making process", "Someone else is involved financially and in decision-making", "Not sure"]},
            {"id": "decision_makers_details", "type": "long_text", "label": "Please provide their name and explain how they will be involved.", "required": True, "locked": False,
             "depends_on": {"question_id": "decision_makers", "values": ["Someone else is financially involved", "Someone else is involved in the decision-making process", "Someone else is involved financially and in decision-making"]}},

            {"id": "buyer_situation", "type": "multi_select", "label": "How would you describe yourself? Select all that apply.", "required": False, "locked": False,
             "section_title": "Current Buyer Situation",
             "options": ["First-time home buyer", "Currently renting", "Previously owned a home but do not own one now", "Currently own a home", "Need to sell my current home before purchasing", "Other"]},
            {"id": "buyer_situation_other", "type": "short_text", "label": "Please explain", "required": False, "locked": False,
             "depends_on": {"question_id": "buyer_situation", "values": ["Other"]}},
            {"id": "lease_end_certainty", "type": "select", "label": "Do you know your exact lease end date?", "required": False, "locked": False,
             "options": ["I know the exact date", "I am unsure of the exact date"],
             "depends_on": {"question_id": "buyer_situation", "values": ["Currently renting"]}},
            {"id": "lease_end_date", "type": "date", "label": "When does your current lease end?", "required": False, "locked": False,
             "depends_on": {"question_id": "lease_end_certainty", "values": ["I know the exact date"]}},

            {"id": "purchase_timeline", "type": "select", "label": "What is your timeline for finding the right property or home?", "required": False, "locked": False,
             "section_title": "Purchase Timeline",
             "options": ["Immediately", "Within 3 months", "Within 6 months", "Within 1 year", "Next year", "Unsure"]},

            {"id": "property_intro_statement", "type": "statement", "label": "Now, let's discuss what you are looking for in a property.", "required": False, "locked": True},

            {"id": "property_type", "type": "select", "label": "What type of property are you looking for?", "required": False, "locked": False,
             "section_title": "Property Type",
             "options": ["Primary residence", "Second home", "Investment property", "Other"]},
            {"id": "property_type_other", "type": "short_text", "label": "Please describe the property type", "required": False, "locked": False,
             "depends_on": {"question_id": "property_type", "values": ["Other"]}},

            {"id": "desired_bedrooms", "type": "number", "label": "What is your desired number of bedrooms?", "required": False, "locked": False,
             "section_title": "Bedrooms and Bathrooms"},
            {"id": "first_floor_bedroom", "type": "select", "label": "First-floor bedroom preference", "required": False, "locked": False,
             "options": ["A first-floor bedroom is required", "A first-floor bedroom is preferred", "No first-floor bedroom preference"]},
            {"id": "min_full_bathrooms", "type": "number", "label": "What is the minimum number of full bathrooms you require?", "required": False, "locked": False},
            {"id": "min_partial_bathrooms", "type": "number", "label": "What is the minimum number of partial bathrooms you require?", "required": False, "locked": False,
             "helper_text": "Includes half bathrooms or bathrooms without a full tub or shower."},

            {"id": "property_condition", "type": "multi_select", "label": "What property conditions are you comfortable purchasing? Select all that apply.", "required": False, "locked": False,
             "section_title": "Property Condition",
             "options": ["New construction", "Move-in ready", "Light or cosmetic updating", "Some renovation", "Shell or full rehabilitation condition", "Other"]},
            {"id": "property_condition_other", "type": "short_text", "label": "Please describe", "required": False, "locked": False,
             "depends_on": {"question_id": "property_condition", "values": ["Other"]}},

            {"id": "lifestyle_needs", "type": "long_text", "label": "Are there any other important parts of your life that your future home or property should support?", "required": False, "locked": False,
             "section_title": "Lifestyle and Location Needs",
             "helper_text": "For example: working from home, accessibility needs, pets, entertaining, hobbies, commuting, outdoor space, or multigenerational living."},
            {"id": "location_preferences", "type": "long_text", "label": "Are there any specific neighborhoods, municipalities, townships, or school districts where you would like to purchase?", "required": False, "locked": False,
             "helper_text": "Feel free to list more than one."},

            {"id": "financing_intro_statement", "type": "statement", "label": "Almost finished. We just have a few financial questions.", "required": False, "locked": True},

            {"id": "financing_plan", "type": "select", "label": "Will you be financing this purchase?", "required": False, "locked": False,
             "section_title": "Financing and Preapproval",
             "options": ["Yes", "No, I plan to purchase with cash", "I am not sure yet"]},
            {"id": "mortgage_preapproval", "type": "select", "label": "Have you already been preapproved for a mortgage?", "required": False, "locked": False,
             "options": ["Yes", "No", "Preapproval is currently in progress"],
             "depends_on": {"question_id": "financing_plan", "values": ["Yes"]}},
            {"id": "lender_name", "type": "short_text", "label": "Which mortgage lender or company provided your preapproval?", "required": False, "locked": False,
             "depends_on": {"question_id": "mortgage_preapproval", "values": ["Yes"]}},
            {"id": "monthly_payment_comfort", "type": "currency", "label": "What monthly mortgage payment would you be comfortable paying, including property taxes and homeowners insurance?", "required": False, "locked": False,
             "helper_text": "An estimate is acceptable.", "unit": "/month",
             "depends_on": {"question_id": "financing_plan", "values": ["Yes"]}},

            {"id": "purchase_price_range", "type": "contact_group", "label": "Purchase Price Range", "required": False, "locked": True,
             "fields": [
                 {"id": "budget_min", "type": "currency", "label": "Minimum purchase price", "required": False},
                 {"id": "budget_max", "type": "currency", "label": "Maximum purchase price", "required": False},
             ]},

            {"id": "financial_concerns", "type": "long_text", "label": "Do you have any upfront budget or financial concerns?", "required": False, "locked": False,
             "section_title": "Upfront Financial Concerns",
             "helper_text": "For example: down payment, closing costs, repairs or renovations, moving expenses, selling an existing home, monthly payment, property taxes, or homeowners association fees."},

            {"id": "consultation_notes", "type": "long_text", "label": "Are there any other questions or concerns you would like to discuss when we meet?", "required": False, "locked": False,
             "section_title": "Buyer Consultation"},
        ],
    },
    "showcase_request": {
        "slug": "showcase-request",
        "title": "Setup My Own Showcase",
        "description": "For open house guests who want their own personalized property showcase - reached via the QR code on your Brag Sheet handout. Submitting this form automatically builds (or updates) the guest's Showcase. Its questions and link are locked so that always works, but you can still theme its color and dark mode.",
        "headline": "Setup My Own Showcase",
        "subtitle": "Answer a few quick questions and your personalized Showcase of matching homes builds itself.",
        "success_heading": "Your Showcase is on its way!",
        "success_message": "We're building your personalized Showcase right now based on what you told us - keep an eye on your email.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            # Gates the rest of the form - a Showcase only makes sense for someone not already
            # being helped by another agent, and building one for a represented buyer anyway
            # would step on that agent's relationship. Every question below depends on "No" here,
            # so a "Yes" answer skips straight past all of them to the apology statement -
            # submit_form/forms_service.py also refuses to build a Showcase server-side even if
            # this were somehow bypassed.
            {"id": "has_agent", "type": "select", "label": "Do you already have an agent helping you buy?", "required": True, "locked": True,
             "options": ["No, I'm not working with an agent", "Yes, I already have an agent"]},
            {"id": "has_agent_stop", "type": "statement", "label": "We're not able to build you a Showcase, since you're already working with an agent - reach out to them directly for anything you need. Thanks for stopping by!", "required": False, "locked": True, "terminal": True,
             "depends_on": {"question_id": "has_agent", "values": ["Yes, I already have an agent"]}},
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone", "required": True},
             ]},
            {"id": "area", "type": "contact_group", "label": "Where are you looking?", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "cities", "type": "city_multiselect", "label": "Cities / Towns", "required": False},
                 {"id": "school_districts", "type": "school_district_multiselect", "label": "School Districts", "required": False},
                 {"id": "county", "type": "county_multiselect", "label": "County", "required": False},
                 {"id": "state", "type": "select", "label": "State", "required": False, "searchable": True, "options": US_STATE_NAMES},
             ]},
            {"id": "price_range", "type": "contact_group", "label": "Price Range", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "budget_min", "type": "currency", "label": "Minimum price", "required": False},
                 {"id": "budget_max", "type": "currency", "label": "Maximum price", "required": False},
             ]},
            {"id": "beds_baths", "type": "contact_group", "label": "How many bedrooms do you need?", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "min_bedrooms", "type": "number", "label": "Minimum bedrooms", "required": False},
             ]},
            {"id": "acres", "type": "contact_group", "label": "Lot size (acres)", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "min_acres", "type": "number", "label": "Minimum acres", "required": False},
                 {"id": "max_acres", "type": "number", "label": "Maximum acres", "required": False},
             ]},
            {"id": "sqft_range", "type": "contact_group", "label": "Living area (sq ft)", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "min_sqft", "type": "number", "label": "Minimum sq ft", "required": False},
                 {"id": "max_sqft", "type": "number", "label": "Maximum sq ft", "required": False},
             ]},
            {"id": "build_year", "type": "contact_group", "label": "Build year", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "fields": [
                 {"id": "min_year_built", "type": "number", "label": "Built in or after", "required": False},
                 {"id": "max_year_built", "type": "number", "label": "Built in or before", "required": False},
             ]},
            {"id": "home_type", "type": "multi_select", "label": "What type of property are you looking for?", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "options": ["Single-family home", "Condo/Townhouse", "Multi-family", "Land", "Other"]},
            {"id": "notes", "type": "long_text", "label": "Anything else we should know?", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]}},
            {"id": "key_features", "type": "multi_select", "label": "What key features would you like?", "required": False, "locked": True,
             "depends_on": {"question_id": "has_agent", "values": ["No, I'm not working with an agent"]},
             "options": ["Pool", "Cul-de-sac", "First floor primary suite", "Garage", "Fenced yard", "Open floor plan", "Updated kitchen", "Waterfront", "Finished basement", "New construction"]},
        ],
    },
    "make_me_sell": {
        "slug": "make-me-sell",
        "title": "Make Me Sell",
        "description": "A seller lead-gen form built for mailers - asks a homeowner what it would actually take to get them to sell, so you can follow up and tell them whether their number is realistic in today's market.",
        "headline": "Name Your Price - We'll Tell You If It's Possible",
        "subtitle": "Every home has a price that makes an owner say yes. Tell us yours, and we'll let you know what's realistic in today's market.",
        "success_heading": "Got it!",
        "success_message": "I'll take a look at your numbers against today's market and follow up soon.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone", "required": True},
             ]},
            {"id": "property_address", "type": "address_autocomplete", "label": "What's the address of the property?", "required": True, "locked": True},
            {"id": "dream_price", "type": "currency", "label": "If you could get any price, what's your dream number?", "required": False, "locked": False},
            {"id": "would_sell_price", "type": "currency", "label": "What price would make you actually list and sell?", "required": True, "locked": False},
            {"id": "would_settle_price", "type": "currency", "label": "What's the lowest you'd realistically settle for?", "required": False, "locked": False},
            {"id": "timeline", "type": "select", "label": "How soon are you thinking about selling?", "required": False, "locked": False,
             "options": ["Right now", "In the next 3-6 months", "In the next 6-12 months", "Just curious what it's worth"]},
            {"id": "notes", "type": "long_text", "label": "Anything else we should know?", "required": False, "locked": True},
        ],
    },
}

SHOWCASE_BUILDING_FORM_TYPES = {"buying", "how_can_i_help"}


# The forms' own questions that the Showcase block now asks better (structured, so the search can
# use them) - dropped so nobody is asked the same thing twice. How Can We Help's bedrooms+bathrooms
# question keeps only bathrooms (the Showcase block asks bedrooms).
SHOWCASE_DUPLICATE_IDS = {
    "buying": {"areas", "price_range"},
    "how_can_i_help": {"areas", "price_range", "buy_property_type"},
}
BUY_BATHROOMS_ONLY = {
    "id": "buy_beds_baths", "type": "contact_group", "label": "How many bathrooms do you need?", "required": False, "locked": False,
    "depends_on": {"question_id": "intent", "values": ["I'm looking to buy"]},
    "fields": [{"id": "buy_bathrooms", "type": "number", "label": "Bathrooms", "required": False}],
}


# Schedule a Showing forms made before the day-and-time picker: one "Schedule a Showing" group (or
# loose questions) asking for the address, name, phone, email and a typed "when are you available".
# Those can't cross out the agent's busy times, so they move onto today's questions (user, 10/18) -
# the picker flow then runs (ShowingRequestFlow). Only forms made of these standard questions; one
# with any question the agent added stays as it is. Past requests keep their own answer snapshots.
_LEGACY_SHOWING_IDS = {"showing_details", "property_of_interest", "full_name", "phone", "email", "availability",
                       "preferred_times", "contact_info", "message", "showing_note"}


def upgrade_legacy_showing(form_type: str, questions: List[dict]) -> Optional[List[dict]]:
    """Today's Schedule a Showing questions for an old standard showing form, else None."""
    if form_type != "showing":
        return None
    asked = [q for q in (questions or []) if q.get("type") != "statement"]
    ids = {q.get("id") for q in asked}
    flow_ids = {"property_of_interest", "preferred_times", "contact_info"}
    if "property_of_interest" in ids and ids <= flow_ids:
        return None   # already today's shape
    every = set(ids)
    for q in asked:
        every |= {f.get("id") for f in (q.get("fields") or [])}
    allowed = _LEGACY_SHOWING_IDS | {f"preferred_{k}_{n}" for k in ("date", "time") for n in (1, 2, 3)}
    if not asked or "property_of_interest" not in every or not every <= allowed:
        return None
    return [dict(q) for q in DEFAULT_FORMS["showing"]["questions"]]


def add_showcase_criteria(form_type: str, questions: List[dict]) -> Optional[List[dict]]:
    """I'm Buying and How Can We Help build the buyer a Showcase too, so they carry Setup My Own
    Showcase's home-search questions (slotted in after the buyer's "working with an agent?"
    answer, which gates them), and drop their own questions those duplicate. Returns the new
    question list, or None when the form is already up to date. Used for the defaults below AND to
    upgrade forms agents created before this existed; safe to run on every load."""
    if form_type not in SHOWCASE_BUILDING_FORM_TYPES:
        return None
    out = [dict(q) for q in questions]
    ids = {q.get("id") for q in out}
    changed = False
    if "sc_price_range" not in ids:
        out = _insert_showcase_block(form_type, out, ids)
        changed = True

    if form_type == "how_can_i_help":
        i = next((n for n, q in enumerate(out) if q.get("id") == "buy_beds_baths"), None)
        if i is not None and any(f.get("id") == "buy_bedrooms" for f in out[i].get("fields") or []):
            out.pop(i)
            at = next((n + 1 for n, q in enumerate(out) if q.get("id") == "sc_beds"), len(out))
            out.insert(at, {**BUY_BATHROOMS_ONLY, "fields": [dict(f) for f in BUY_BATHROOMS_ONLY["fields"]]})
            changed = True

    dups = SHOWCASE_DUPLICATE_IDS[form_type]
    section = next((q.get("section_title") for q in out if q.get("id") in dups and q.get("section_title")), None)
    kept = [q for q in out if q.get("id") not in dups]
    if len(kept) != len(out):
        out, changed = kept, True
        if section:
            # The removed question opened the "Buying" section - the next buyer question opens it now.
            first = next((q for q in out if (q.get("depends_on") or {}).get("question_id") == "intent"
                          and "I'm looking to buy" in (q.get("depends_on") or {}).get("values", [])), None)
            if first is not None and not first.get("section_title"):
                first["section_title"] = section

    return out if changed else None


def _insert_showcase_block(form_type: str, questions: List[dict], ids: set) -> List[dict]:
    out = questions

    def index_after(*candidates: str) -> int:
        for cid in candidates:
            for i, q in enumerate(out):
                if q.get("id") == cid:
                    return i + 1
        return len(out)

    if form_type == "buying":
        block = [dict(BUYING_AGENT_GATE)] + showcase_criteria_questions("sc_has_agent", [BUYING_AGENT_GATE["options"][0]])
        at = index_after("pre_approved", "timeline", "price_range", "areas", "contact_info")
    else:
        gate = next((q for q in out if q.get("id") == "buy_working_with_agent"), None)
        if gate is None:
            # The agent deleted their own agent question - bring the Showcase one in for buyers.
            gate_q = dict(BUYING_AGENT_GATE, depends_on={"question_id": "intent", "values": ["I'm looking to buy"]})
            block = [gate_q] + showcase_criteria_questions("sc_has_agent", [BUYING_AGENT_GATE["options"][0]])
            at = index_after("buy_timeline", "buy_beds_baths", "buy_property_type", "price_range", "areas", "contact_info")
        else:
            # It now decides whether they get a Showcase, so it has to be answered.
            gate["required"] = True
            gate["locked"] = True
            block = showcase_criteria_questions("buy_working_with_agent", HOW_CAN_I_HELP_GATE_VALUES)
            at = index_after("buy_working_with_agent")
    block = [q for q in block if q["id"] not in ids]
    return out[:at] + block + out[at:]


for _ft in SHOWCASE_BUILDING_FORM_TYPES:
    DEFAULT_FORMS[_ft]["questions"] = add_showcase_criteria(_ft, DEFAULT_FORMS[_ft]["questions"])


# Submitting this form drives automatic Showcase creation/update (see
# create_or_update_showcase_from_submission), which depends on its specific questions/fields, and
# it's reached via a printed QR code on the Brag Sheet, so the link can't change either. Agents can
# still theme it (accent color, dark mode, branding) - see AESTHETICS_ONLY_FORM_TYPES below - but
# not its title/copy/questions.
URL_LOCKED_FORM_TYPES = {"showcase_request"}
AESTHETICS_ONLY_FORM_TYPES = {"showcase_request"}

# Campaigns (separately-tracked links/QR codes for the same premade form) are scoped to normal
# premade forms only - showcase_request already has its own locked/shared URL mechanics
# (URL_LOCKED_FORM_TYPES above), and the two onboarding forms have their own restricted-editing
# story; both keep today's single-link behavior untouched rather than being designed around here.
CAMPAIGN_ELIGIBLE_FORM_TYPES = set(DEFAULT_FORMS) - {"showcase_request", "seller_onboarding", "buyer_onboarding"}


def _is_eligible_sale(sale: dict) -> bool:
    status_text = str(sale.get("status") or "").replace("-BRIGHT", "").replace("_", " ").strip().lower()
    is_sold = "sold" in status_text or "closed" in status_text
    return is_sold and (sale.get("price") or 0) >= 100000


def _get_example_properties_for_showcase(hub_config: dict, limit: int = 4) -> Optional[List[ExampleProperty]]:
    """A few real sold homes from the agent's own Recent Sales (already cached on their Agent Hub
    config, same data the public Hub page's Recent Sales carousel renders) - used to illustrate
    the Showcase Request welcome-screen preview with actual houses instead of placeholder tiles."""
    raw_sales = hub_config.get("recentSales")
    if not isinstance(raw_sales, list):
        return None
    eligible = [sale for sale in raw_sales if isinstance(sale, dict) and _is_eligible_sale(sale)]
    if not eligible:
        return None
    return [
        ExampleProperty(address=sale.get("address") or "", city=sale.get("city"), price=sale.get("price"), image=sale.get("image"))
        for sale in eligible[:limit]
    ]


def normalize_form_slug(slug: str) -> str:
    return slug.strip().lower()


def validate_form_slug(slug: str) -> str:
    normalized = normalize_form_slug(slug)
    if not SLUG_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use letters, numbers, and hyphens only. No spaces or unsupported symbols.",
        )
    if normalized in RESERVED_FORM_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This link ending is reserved. Please choose another.",
        )
    return normalized


async def get_or_create_forms(db: AsyncSession, owner_id: str) -> List[AgentForm]:
    result = await db.execute(select(AgentForm).where(AgentForm.owner_id == owner_id))
    all_forms = result.scalars().all()
    existing_default_types = {form.form_type for form in all_forms if form.form_type in DEFAULT_FORMS}
    missing_types = [form_type for form_type in DEFAULT_FORMS if form_type not in existing_default_types]

    if missing_types:
        next_order = max((form.order_index for form in all_forms), default=-1) + 1
        for form_type in missing_types:
            defaults = DEFAULT_FORMS[form_type]
            db.add(AgentForm(
                owner_id=owner_id,
                form_type=form_type,
                slug=defaults["slug"],
                title=defaults["title"],
                headline=defaults["headline"],
                subtitle=defaults["subtitle"],
                description=defaults.get("description"),
                questions=[dict(question) for question in defaults["questions"]],
                success_heading=defaults["success_heading"],
                success_message=defaults["success_message"],
                accent_color=defaults["accent_color"],
                order_index=next_order,
            ))
            next_order += 1

        await db.commit()
        result = await db.execute(select(AgentForm).where(AgentForm.owner_id == owner_id))
        all_forms = result.scalars().all()

    # Aesthetics-only forms (showcase_request) can never have their questions/copy edited by the
    # agent, so unlike every other form type it's always safe - and necessary, since nothing else
    # keeps already-created rows current - to resync existing rows with the latest DEFAULT_FORMS
    # content on every load. Theme fields (accent_color, dark_mode, branding, ...) are agent-owned
    # and deliberately left untouched here.
    needs_resync = False
    for form in all_forms:
        if form.form_type not in AESTHETICS_ONLY_FORM_TYPES:
            continue
        defaults = DEFAULT_FORMS[form.form_type]
        default_questions = [dict(question) for question in defaults["questions"]]
        for attr, default_value in [
            ("questions", default_questions),
            ("title", defaults["title"]),
            ("headline", defaults["headline"]),
            ("subtitle", defaults["subtitle"]),
            ("description", defaults.get("description")),
            ("success_heading", defaults["success_heading"]),
            ("success_message", defaults["success_message"]),
        ]:
            if getattr(form, attr) != default_value:
                setattr(form, attr, default_value)
                needs_resync = True

    # Old standard Schedule a Showing forms move onto the day-and-time picker.
    for form in all_forms:
        upgraded = upgrade_legacy_showing(form.form_type, form.questions or [])
        if upgraded is not None:
            form.questions = upgraded
            needs_resync = True

    # Forms made before I'm Buying / How Can We Help built Showcases get the home-search block.
    for form in all_forms:
        upgraded = add_showcase_criteria(form.form_type, form.questions or [])
        if upgraded is not None:
            form.questions = upgraded
            needs_resync = True

    for form in all_forms:
        if form.form_type != "how_can_i_help":
            continue
        if form.title == "How Can I Help?":
            form.title = DEFAULT_FORMS["how_can_i_help"]["title"]
            needs_resync = True
        if form.headline == "How Can I Help?":
            form.headline = DEFAULT_FORMS["how_can_i_help"]["headline"]
            needs_resync = True

    if needs_resync:
        await db.commit()
        result = await db.execute(select(AgentForm).where(AgentForm.owner_id == owner_id))
        all_forms = result.scalars().all()

    return sorted(all_forms, key=lambda form: form.order_index)


async def get_owned_form(db: AsyncSession, form_id: str, owner_id: str) -> AgentForm:
    result = await db.execute(select(AgentForm).where(AgentForm.id == form_id, AgentForm.owner_id == owner_id))
    form = result.scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Form not found")
    return form


def _require_campaign_eligible(form: AgentForm) -> None:
    if form.form_type not in CAMPAIGN_ELIGIBLE_FORM_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This form doesn't support campaigns.")


async def get_owned_campaign(db: AsyncSession, form_id: str, campaign_id: str, owner_id: str) -> FormCampaign:
    """Deliberately does NOT filter on deleted_at - a deleted campaign's own stats/lookup must
    keep working (history stays viewable), only its token stops resolving for NEW traffic
    (forms_service.resolve_campaign_id handles that half separately)."""
    result = await db.execute(
        select(FormCampaign).where(
            FormCampaign.id == campaign_id,
            FormCampaign.form_id == form_id,
            FormCampaign.owner_id == owner_id,
        )
    )
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return campaign


async def _generate_campaign_token(db: AsyncSession, form_id: str) -> str:
    for _ in range(5):
        token = uuid.uuid4().hex[:10]
        result = await db.execute(
            select(FormCampaign.id).where(FormCampaign.form_id == form_id, FormCampaign.token == token)
        )
        if result.scalar_one_or_none() is None:
            return token
    # A 10-char hex collision on the same form 5 times in a row is effectively impossible - if it
    # ever happens, fail loudly rather than silently reuse a token across two campaigns.
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not generate a unique campaign link. Try again.")


async def _campaign_counts(db: AsyncSession, form_id: str) -> Dict[Optional[str], Dict[str, int]]:
    """One grouped query per table, joined in Python - same shape/cost as _build_stats_payload's
    own approach. Keyed by campaign_id (None = General)."""
    counts: Dict[Optional[str], Dict[str, int]] = defaultdict(lambda: {"views": 0, "leads": 0})
    views_result = await db.execute(
        select(FormViewSession.campaign_id, func.count()).where(FormViewSession.form_id == form_id).group_by(FormViewSession.campaign_id)
    )
    for campaign_id, count in views_result.all():
        counts[campaign_id]["views"] = count
    leads_result = await db.execute(
        select(FormSubmission.campaign_id, func.count()).where(FormSubmission.form_id == form_id).group_by(FormSubmission.campaign_id)
    )
    for campaign_id, count in leads_result.all():
        counts[campaign_id]["leads"] = count
    return counts


async def _campaign_breakdown(db: AsyncSession, form_id: str) -> List[dict]:
    """For the "All Campaigns" stats view - General plus every named campaign, active AND deleted,
    all included (a combined total is not a total-minus-deleted). Reuses _campaign_counts rather
    than a fourth query shape for the same numbers."""
    counts = await _campaign_counts(db, form_id)
    result = await db.execute(select(FormCampaign.id, FormCampaign.name, FormCampaign.deleted_at).where(FormCampaign.form_id == form_id))
    meta = {campaign_id: (name, deleted_at is not None) for campaign_id, name, deleted_at in result.all()}
    breakdown = [{
        "campaignId": None, "name": "General", "isDeleted": False,
        "views": counts.get(None, {}).get("views", 0), "leads": counts.get(None, {}).get("leads", 0),
    }]
    for campaign_id, data in counts.items():
        if campaign_id is None:
            continue
        name, is_deleted = meta.get(campaign_id, ("Unknown campaign", True))
        breakdown.append({"campaignId": campaign_id, "name": name, "isDeleted": is_deleted, "views": data["views"], "leads": data["leads"]})
    return breakdown


@router.get("/me", response_model=List[AgentFormResponse])
async def get_my_forms(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    forms = await get_or_create_forms(db, current_user.id)
    # One grouped count query for every eligible form at once (not N+1 per card) - counts.py
    # convention already established elsewhere in this file (see _campaign_counts). Set as a
    # transient attribute since additional_campaign_count has no real column - AgentFormResponse's
    # from_attributes=True picks it up the same way it reads every other field off the ORM object.
    result = await db.execute(
        select(FormCampaign.form_id, func.count())
        .where(FormCampaign.owner_id == current_user.id, FormCampaign.deleted_at.is_(None))
        .group_by(FormCampaign.form_id)
    )
    counts_by_form = dict(result.all())
    for form in forms:
        form.additional_campaign_count = counts_by_form.get(form.id, 0)
    return forms


@router.get("/{form_id}", response_model=AgentFormResponse)
async def get_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Thin wrapper around get_owned_form so the per-form management page can load a single form
    directly (by id, from a bookmarked/shared link) without re-fetching the whole list."""
    form = await get_owned_form(db, form_id, current_user.id)
    result = await db.execute(
        select(func.count()).select_from(FormCampaign).where(FormCampaign.form_id == form_id, FormCampaign.deleted_at.is_(None))
    )
    form.additional_campaign_count = result.scalar() or 0
    return form


STATS_RANGES = {7, 30, 90, 0}  # days; 0 = all time


def _in_range(sessions: List[FormViewSession], days: int) -> List[FormViewSession]:
    if not days:
        return list(sessions)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return [s for s in sessions if s.created_at and s.created_at >= since]


def _build_stats_payload(sessions: List[FormViewSession], questions: List[dict], campaign_breakdown: Optional[List[dict]] = None,
                         days: Optional[int] = None) -> dict:
    """Shared by the per-form, per-campaign, General-only, and owner-wide summary stats endpoints
    - `questions` is empty for the summary (a drop-off funnel only makes sense for one form's
    fixed question order). `campaign_breakdown` is only passed for the "All Campaigns" combined
    view - per-campaign/General-only calls pre-filter `sessions` instead and leave this unset, so
    the payload shape for those stays identical to today's (no empty/meaningless breakdown key)."""
    total_views = len(sessions)
    total_starts = sum(1 for s in sessions if s.furthest_step_index >= 1)
    total_completions = sum(1 for s in sessions if s.completed_at is not None)
    completion_rate = round((total_completions / total_views) * 100, 1) if total_views else 0.0

    since = datetime.now(timezone.utc) - timedelta(days=30)
    daily_views: Dict[str, int] = defaultdict(int)
    daily_completions: Dict[str, int] = defaultdict(int)
    for s in sessions:
        if s.created_at and s.created_at >= since:
            daily_views[s.created_at.date().isoformat()] += 1
        if s.completed_at and s.completed_at >= since:
            daily_completions[s.completed_at.date().isoformat()] += 1

    question_drop_off = []
    for index, question in enumerate(questions, start=1):
        reached = sum(1 for s in sessions if s.furthest_step_index >= index)
        question_drop_off.append({
            "questionId": question.get("id"),
            "label": question.get("label"),
            "reachedCount": reached,
            "reachedPercent": round((reached / total_views) * 100, 1) if total_views else 0.0,
        })

    payload = {
        "totalViews": total_views,
        "totalStarts": total_starts,
        "totalCompletions": total_completions,
        "completionRate": completion_rate,
        "dailyViews": [{"date": d, "count": c} for d, c in sorted(daily_views.items())],
        "dailyCompletions": [{"date": d, "count": c} for d, c in sorted(daily_completions.items())],
        "questionDropOff": question_drop_off,
    }
    if questions and days is not None:
        # The full insights (per-question drop-off from each visit's real path - right on
        # branching forms too - sources, devices, day-by-day drop-offs...). `sessions` is already
        # cut to the chosen range by the caller.
        from app.services.form_insights_service import build_insights
        insights = build_insights(sessions, questions, days=days)
        payload.update(insights)
        payload["questionDropOff"] = insights["questions"]
    if campaign_breakdown is not None:
        payload["campaignBreakdown"] = campaign_breakdown
    return payload


@router.get("/stats/summary")
async def get_forms_stats_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Accumulated views/completions across every one of the owner's forms - powers the summary
    strip at the top of the Forms tab. No per-question funnel (that's only meaningful per-form)."""
    result = await db.execute(select(FormViewSession).where(FormViewSession.owner_id == current_user.id))
    sessions = result.scalars().all()
    return _build_stats_payload(sessions, [])


@router.get("/{form_id}/stats")
async def get_form_stats(
    form_id: str,
    days: int = Query(30, description="7, 30, 90, or 0 for all time"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """"All Campaigns" combined - every session for the form regardless of campaign_id, General
    and every named campaign (active and deleted) all included. Distinct from both a single named
    campaign's stats and from General-only stats below - never conflate the three."""
    form = await get_owned_form(db, form_id, current_user.id)
    days = days if days in STATS_RANGES else 30
    result = await db.execute(select(FormViewSession).where(FormViewSession.form_id == form_id))
    sessions = _in_range(result.scalars().all(), days)
    breakdown = await _campaign_breakdown(db, form_id) if form.form_type in CAMPAIGN_ELIGIBLE_FORM_TYPES else None
    if breakdown is not None and days:
        # Same range as everything else on the page: views/submissions per campaign from the
        # visits in range (names/deleted flags still come from the campaigns themselves).
        views: Dict[Optional[str], int] = defaultdict(int)
        done: Dict[Optional[str], int] = defaultdict(int)
        for sess in sessions:
            views[sess.campaign_id] += 1
            done[sess.campaign_id] += 1 if sess.completed_at else 0
        breakdown = [dict(b, views=views.get(b["campaignId"], 0), leads=done.get(b["campaignId"], 0)) for b in breakdown]
    return _build_stats_payload(sessions, form.questions, campaign_breakdown=breakdown, days=days)


@router.get("/campaigns/{campaign_id}", response_model=CampaignLookupResponse)
async def lookup_campaign(
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Resolves a campaign_id to its name/token plus its owning form's id/slug/title - the one
    thing FormCampaign.form_id doesn't save a caller from looking up. Used by Design Studio to
    display "Campaign: X under Form: Y" and build the tracking link for a design that already has
    a campaign_id set, without the agent re-picking the form."""
    result = await db.execute(
        select(FormCampaign, AgentForm.slug, AgentForm.title)
        .join(AgentForm, AgentForm.id == FormCampaign.form_id)
        .where(FormCampaign.id == campaign_id, FormCampaign.owner_id == current_user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    campaign, form_slug, form_title = row
    return CampaignLookupResponse(
        id=campaign.id, name=campaign.name, token=campaign.token,
        form_id=campaign.form_id, form_slug=form_slug, form_title=form_title,
        redirect_mode=campaign.redirect_mode, redirect_url=campaign.redirect_url,
    )


@router.get("/{form_id}/campaigns", response_model=List[FormCampaignResponse])
async def list_form_campaigns(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    form = await get_owned_form(db, form_id, current_user.id)
    _require_campaign_eligible(form)
    counts = await _campaign_counts(db, form_id)
    general_counts = counts.get(None, {"views": 0, "leads": 0})
    campaigns = [FormCampaignResponse(
        id=None, name="General", token=None, is_general=True,
        description="Your everyday link for this form.",
        views=general_counts["views"], leads=general_counts["leads"],
    )]
    result = await db.execute(
        select(FormCampaign).where(FormCampaign.form_id == form_id).order_by(FormCampaign.deleted_at.is_not(None), FormCampaign.created_at)
    )
    for campaign in result.scalars().all():
        campaign_counts = counts.get(campaign.id, {"views": 0, "leads": 0})
        campaigns.append(FormCampaignResponse(
            id=campaign.id, name=campaign.name, token=campaign.token, is_general=False,
            views=campaign_counts["views"], leads=campaign_counts["leads"],
            deleted_at=campaign.deleted_at, created_at=campaign.created_at,
            redirect_mode=campaign.redirect_mode, redirect_url=campaign.redirect_url,
        ))
    return campaigns


@router.post("/{form_id}/campaigns", response_model=FormCampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_form_campaign(
    form_id: str,
    payload: FormCampaignCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    form = await get_owned_form(db, form_id, current_user.id)
    _require_campaign_eligible(form)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give this campaign a name.")
    redirect_mode, redirect_url = normalize_campaign_redirect(payload.redirect_mode, payload.redirect_url)
    token = await _generate_campaign_token(db, form_id)
    campaign = FormCampaign(form_id=form_id, owner_id=current_user.id, name=name, token=token, redirect_mode=redirect_mode, redirect_url=redirect_url)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return FormCampaignResponse(id=campaign.id, name=campaign.name, token=campaign.token, is_general=False, created_at=campaign.created_at,
                                redirect_mode=campaign.redirect_mode, redirect_url=campaign.redirect_url)


@router.patch("/{form_id}/campaigns/{campaign_id}", response_model=FormCampaignResponse)
async def update_form_campaign(
    form_id: str,
    campaign_id: str,
    payload: FormCampaignUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Where this campaign's visitors go after submitting - takes effect on the very next visit,
    so already-printed QR codes follow the new choice."""
    campaign = await get_owned_campaign(db, form_id, campaign_id, current_user.id)
    if campaign.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This campaign was deleted.")
    campaign.redirect_mode, campaign.redirect_url = normalize_campaign_redirect(payload.redirect_mode, payload.redirect_url)
    await db.commit()
    await db.refresh(campaign)
    return FormCampaignResponse(id=campaign.id, name=campaign.name, token=campaign.token, is_general=False, created_at=campaign.created_at,
                                redirect_mode=campaign.redirect_mode, redirect_url=campaign.redirect_url)


@router.delete("/{form_id}/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_form_campaign(
    form_id: str,
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Soft delete only - sets deleted_at, never removes the row or touches any
    FormViewSession/FormSubmission that already points at it, so historical stats/leads keep
    working under this campaign's real name forever. Only effect on the future: its token stops
    resolving in forms_service.resolve_campaign_id, so any traffic still hitting an old printed
    link/QR for it silently falls back to General instead of erroring or reviving the campaign."""
    campaign = await get_owned_campaign(db, form_id, campaign_id, current_user.id)
    campaign.deleted_at = datetime.now(timezone.utc)
    await db.commit()


@router.get("/{form_id}/campaigns/general/stats")
async def get_general_campaign_stats(
    form_id: str,
    days: int = Query(30),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """General-only - campaign_id IS NULL traffic, nothing else. Must be declared before the
    {campaign_id}/stats route below so the literal "general" segment matches here first instead of
    being captured as a campaign id."""
    form = await get_owned_form(db, form_id, current_user.id)
    _require_campaign_eligible(form)
    days = days if days in STATS_RANGES else 30
    result = await db.execute(select(FormViewSession).where(FormViewSession.form_id == form_id, FormViewSession.campaign_id.is_(None)))
    sessions = _in_range(result.scalars().all(), days)
    return _build_stats_payload(sessions, form.questions, days=days)


@router.get("/{form_id}/campaigns/{campaign_id}/stats")
async def get_campaign_stats(
    form_id: str,
    campaign_id: str,
    days: int = Query(30),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Works for a deleted campaign too - history stays fully viewable, only new attribution
    stops."""
    form = await get_owned_form(db, form_id, current_user.id)
    _require_campaign_eligible(form)
    await get_owned_campaign(db, form_id, campaign_id, current_user.id)
    days = days if days in STATS_RANGES else 30
    result = await db.execute(select(FormViewSession).where(FormViewSession.form_id == form_id, FormViewSession.campaign_id == campaign_id))
    sessions = _in_range(result.scalars().all(), days)
    return _build_stats_payload(sessions, form.questions, days=days)


def _answer_rows(questions: List[dict], answers: dict) -> List[dict]:
    """What they answered, in the form's own order, with the question text (group fields read
    "Group - Field")."""
    rows = []
    for q in questions or []:
        if q.get("type") == "statement":
            continue
        if q.get("type") == "contact_group":
            for f in q.get("fields") or []:
                if f.get("id") in answers:
                    rows.append({"questionId": f["id"], "label": f.get("label") or f["id"], "group": q.get("label"), "value": answers[f["id"]]})
        elif q.get("id") in answers:
            rows.append({"questionId": q["id"], "label": q.get("label") or q["id"], "group": None, "value": answers[q["id"]]})
    return rows


@router.get("/{form_id}/unfinished")
async def get_unfinished_responses(
    form_id: str,
    campaign: Optional[str] = Query(None, description="unset = all, 'general', or a campaign id"),
    days: int = Query(0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Visits where someone started answering but never pressed Submit - what they did answer,
    the question they stopped on, when, and where they came from. Answers are kept
    UNFINISHED_RETENTION_DAYS, then cleared nightly (the visit still counts in the stats)."""
    from app.services.form_insights_service import ABANDON_AFTER, question_order, session_path
    from app.services.forms_service import UNFINISHED_RETENTION_DAYS
    form = await get_owned_form(db, form_id, current_user.id)
    stmt = select(FormViewSession).where(
        FormViewSession.form_id == form_id,
        FormViewSession.completed_at.is_(None),
        FormViewSession.dismissed_at.is_(None),
        FormViewSession.partial_answers.is_not(None),
    )
    if campaign == "general":
        stmt = stmt.where(FormViewSession.campaign_id.is_(None))
    elif campaign:
        stmt = stmt.where(FormViewSession.campaign_id == campaign)
    rows = (await db.execute(stmt.order_by(FormViewSession.last_activity_at.desc().nullslast()).limit(300))).scalars().all()
    rows = _in_range(rows, days if days in STATS_RANGES else 0)

    names = dict((await db.execute(select(FormCampaign.id, FormCampaign.name).where(FormCampaign.form_id == form_id))).all())
    # Did they come back and submit later (same email/phone)? Then they're not really unfinished.
    subs = (await db.execute(select(FormSubmission.contact_email, FormSubmission.contact_phone, FormSubmission.created_at)
                             .where(FormSubmission.form_id == form_id))).all()
    digits = lambda v: "".join(ch for ch in (v or "") if ch.isdigit())[-10:]

    qs = question_order(form.questions)
    labels = {q["id"]: q.get("label") for q in qs}
    number = {q["id"]: i for i, q in enumerate(qs, start=1)}
    now = datetime.now(timezone.utc)
    # Already added to Leads (the "Add to Leads" button) -> that lead's id.
    from app.models.database import LeadEvent
    from app.services.forms_service import PARTIAL_SOURCE
    added = dict((await db.execute(select(LeadEvent.source_id, LeadEvent.lead_id).where(
        LeadEvent.source_type == PARTIAL_SOURCE, LeadEvent.source_id.in_([s.id for s in rows] or [""])))).all())
    items = []
    for s in rows:
        answers = s.partial_answers if isinstance(s.partial_answers, dict) else {}
        if not answers:
            continue
        path = session_path(s, form.questions)
        last_q = path[-1][0] if path else s.furthest_question_id
        email, phone = (s.contact_email or "").lower(), digits(s.contact_phone)
        finished_later = any(
            sub_at and s.created_at and sub_at >= s.created_at and (
                (email and (sub_email or "").lower() == email) or (phone and digits(sub_phone) == phone))
            for sub_email, sub_phone, sub_at in subs
        )
        last = s.last_activity_at or s.updated_at or s.created_at
        items.append({
            "id": s.id,
            "contactName": s.contact_name,
            "contactEmail": s.contact_email,
            "contactPhone": s.contact_phone,
            "answers": _answer_rows(form.questions, answers),
            "answeredCount": len(answers),
            "lastQuestionId": last_q,
            "lastQuestionLabel": labels.get(last_q),
            "lastQuestionNumber": number.get(last_q),
            "totalQuestions": len(qs),
            "startedAt": (s.started_at or s.created_at).isoformat() if (s.started_at or s.created_at) else None,
            "lastActivityAt": last.isoformat() if last else None,
            "inProgress": bool(last) and now - last < ABANDON_AFTER,
            "finishedLater": finished_later,
            "leadId": added.get(s.id),
            "source": s.source,
            "device": s.device,
            "referrer": s.referrer,
            "campaignName": names.get(s.campaign_id) if s.campaign_id else "General",
        })
    return {"items": items, "retentionDays": UNFINISHED_RETENTION_DAYS}


@router.post("/{form_id}/unfinished/{session_id}/add-to-leads")
async def add_unfinished_response_to_leads(
    form_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Make an unfinished response a lead (with everything they answered) + its follow-up.
    Safe to press twice - the second time just returns the lead it already made."""
    from app.services.forms_service import add_unfinished_to_leads
    form = await get_owned_form(db, form_id, current_user.id)
    s = (await db.execute(select(FormViewSession).where(
        FormViewSession.id == session_id, FormViewSession.form_id == form_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await add_unfinished_to_leads(db, form, s)


@router.post("/{form_id}/unfinished/{session_id}/dismiss")
async def dismiss_unfinished_response(
    form_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Hide one unfinished response from the list (it still counts in the stats)."""
    await get_owned_form(db, form_id, current_user.id)
    s = (await db.execute(select(FormViewSession).where(
        FormViewSession.id == session_id, FormViewSession.form_id == form_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    s.dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True}


@router.post("/{form_id}/duplicate", response_model=AgentFormResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Always produces a plain, fully-editable Custom Form - regardless of source (custom,
    eligible-premade, onboarding, showcase_request) - with none of the source's restrictions
    carried over. Campaigns are never copied: they belong to the original form's row only, and
    duplication is the one action that's explicitly allowed to fork the form (campaigns
    explicitly never do). Mirrors design_routes.py's duplicate_design pattern."""
    original = await get_owned_form(db, form_id, current_user.id)

    existing_forms = (await db.execute(select(AgentForm).where(AgentForm.owner_id == current_user.id))).scalars().all()
    existing_slugs = {f.slug for f in existing_forms}
    base_slug = f"{original.slug}-copy"
    slug = base_slug
    suffix = 2
    while slug in existing_slugs:
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    next_order = max((f.order_index for f in existing_forms), default=-1) + 1

    copy = AgentForm(
        owner_id=current_user.id,
        form_type="custom",
        slug=slug,
        title=f"{original.title} - Copy",
        headline=original.headline,
        subtitle=original.subtitle,
        description=original.description,
        questions=[dict(q) for q in original.questions],
        success_heading=original.success_heading,
        success_message=original.success_message,
        accent_color=original.accent_color,
        show_branding=original.show_branding,
        dark_mode=original.dark_mode,
        redirect_to_agent_hub=original.redirect_to_agent_hub,
        property_photo_carousel=original.property_photo_carousel,
        background_effects=original.background_effects,
        welcome_text_effects=original.welcome_text_effects,
        branding_layout=original.branding_layout,
        order_index=next_order,
        # Premade sources never had one populated (PROPERTY_LINK_DISABLED_TYPES already blocks it
        # on the original), so only actually carry it over for a custom-to-custom duplicate.
        linked_property=original.linked_property if original.form_type == "custom" else None,
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return copy


@router.post("", response_model=AgentFormResponse, status_code=status.HTTP_201_CREATED)
async def create_form(
    payload: AgentFormCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    title = payload.title.strip() or "Untitled Form"
    base_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "form"
    if base_slug in RESERVED_FORM_SLUGS or not SLUG_PATTERN.fullmatch(base_slug):
        base_slug = f"form-{uuid.uuid4().hex[:6]}"

    existing_forms = (await db.execute(select(AgentForm).where(AgentForm.owner_id == current_user.id))).scalars().all()
    existing_slugs = {form.slug for form in existing_forms}
    slug = base_slug
    suffix = 2
    while slug in existing_slugs:
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    next_order = max((form.order_index for form in existing_forms), default=-1) + 1

    form = AgentForm(
        owner_id=current_user.id,
        form_type="custom",
        slug=slug,
        title=title,
        headline=title,
        subtitle=None,
        questions=[
            {"id": "full_name", "type": "short_text", "label": "Full name", "required": True, "locked": True},
            {"id": "email", "type": "email", "label": "Email", "required": True, "locked": True},
            {"id": "phone", "type": "phone", "label": "Phone", "required": True, "locked": True},
        ],
        success_heading="Thanks!",
        success_message="I'll be in touch soon.",
        accent_color=BRAND_ACCENT,
        order_index=next_order,
    )
    db.add(form)
    await db.commit()
    await db.refresh(form)
    return form


@router.delete("/{form_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    form = await get_owned_form(db, form_id, current_user.id)
    if form.form_type in DEFAULT_FORMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This is a core OpenHousePal form and can't be deleted. Turn it off in My Forms instead.",
        )
    await db.delete(form)
    await db.commit()


@router.patch("/layout", response_model=List[AgentFormResponse])
async def update_forms_layout(
    payload: AgentFormLayoutUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    result = await db.execute(select(AgentForm).where(AgentForm.owner_id == current_user.id))
    forms_by_id = {form.id: form for form in result.scalars().all()}

    for item in payload.forms:
        form = forms_by_id.get(item.id)
        if not form:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Form not found")
        form.order_index = item.order_index
        form.is_enabled = item.is_enabled

    await db.commit()
    result = await db.execute(select(AgentForm).where(AgentForm.owner_id == current_user.id))
    return sorted(result.scalars().all(), key=lambda form: form.order_index)


@router.post("/{form_id}/reset", response_model=AgentFormResponse)
async def reset_form_to_default(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    form = await get_owned_form(db, form_id, current_user.id)
    if form.form_type not in DEFAULT_FORMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only OpenHousePal's premade forms can be reset to default.",
        )

    defaults = DEFAULT_FORMS[form.form_type]
    # Slug, enabled state, and order are left untouched - a reset shouldn't break the form's
    # existing public link/QR code or change where it sits in the agent's Forms list.
    form.title = defaults["title"]
    form.headline = defaults["headline"]
    form.subtitle = defaults["subtitle"]
    form.description = defaults.get("description")
    form.questions = [dict(question) for question in defaults["questions"]]
    form.success_heading = defaults["success_heading"]
    form.success_message = defaults["success_message"]
    form.accent_color = defaults["accent_color"]
    form.show_branding = True
    form.dark_mode = False
    form.redirect_to_agent_hub = True
    form.property_photo_carousel = False
    form.background_effects = True
    form.welcome_text_effects = True
    form.branding_layout = 'split'
    form.linked_property = None

    await db.commit()
    await db.refresh(form)
    return form


@router.get("/{form_id}/slug/check", response_model=SlugCheckResponse)
async def check_form_slug(
    form_id: str,
    slug: str = Query(..., min_length=1, max_length=80),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    normalized = validate_form_slug(slug)
    result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == current_user.id,
            AgentForm.slug == normalized,
            AgentForm.id != form_id,
        )
    )
    taken = result.scalar_one_or_none()
    return SlugCheckResponse(slug=normalized, available=taken is None)


@router.put("/{form_id}/slug", response_model=AgentFormResponse)
async def update_form_slug(
    form_id: str,
    payload: SlugUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    form = await get_owned_form(db, form_id, current_user.id)
    if form.form_type in URL_LOCKED_FORM_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This form's link can't be changed - it's printed on QR codes that would otherwise stop working.")
    normalized = validate_form_slug(payload.slug)

    existing = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == current_user.id,
            AgentForm.slug == normalized,
            AgentForm.id != form_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You're already using this link for another form.")

    form.slug = normalized
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You're already using this link for another form.")

    await db.refresh(form)
    return form


@router.get("/submissions/unread-count")
async def get_unread_submissions_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    result = await db.execute(
        select(func.count()).select_from(FormSubmission).where(
            FormSubmission.owner_id == current_user.id,
            FormSubmission.is_reviewed.is_(False),
        )
    )
    return {"count": result.scalar() or 0}


@router.get("/{form_id}/submissions", response_model=List[FormSubmissionListItem])
async def get_form_submissions(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    await get_owned_form(db, form_id, current_user.id)
    result = await db.execute(
        select(FormSubmission)
        .where(FormSubmission.form_id == form_id, FormSubmission.owner_id == current_user.id)
        .order_by(FormSubmission.created_at.desc())
    )
    submissions = result.scalars().all()
    # Resolved regardless of deleted_at, so a deleted campaign's real name still shows on old
    # submissions instead of going blank. None (General) is left unset - quiet by default, same as
    # the card's meta-line - FormSubmissionListItem.campaign_name stays None for those rows.
    names_result = await db.execute(select(FormCampaign.id, FormCampaign.name).where(FormCampaign.form_id == form_id))
    names = dict(names_result.all())
    for submission in submissions:
        submission.campaign_name = names.get(submission.campaign_id)
    return submissions


@router.patch("/submissions/{submission_id}/reviewed", response_model=FormSubmissionListItem)
async def mark_submission_reviewed(
    submission_id: str,
    payload: SubmissionReviewedUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    result = await db.execute(
        select(FormSubmission).where(FormSubmission.id == submission_id, FormSubmission.owner_id == current_user.id)
    )
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    submission.is_reviewed = payload.is_reviewed

    if payload.is_reviewed:
        # Marking reviewed here is the same "I've seen this" signal as reading the notification
        # in the bell dropdown - without this, the bell/Studio-nav badges stayed stuck on this
        # submission forever even after it was handled from the Forms Results card.
        await db.execute(
            update(Notification)
            .where(
                Notification.reference_type == "FORM_SUBMISSION",
                Notification.reference_id == submission.id,
                Notification.is_read.is_(False),
            )
            .values(is_read=True)
        )
        # ...and the same for the lead it made: the "new" flag on the Dash / Activity lead card
        # (needs_attention = activity since the agent last opened the lead) clears for THIS
        # submission - anything the person did afterwards still shows as new.
        from app.models.database import Lead, LeadEvent
        event = (await db.execute(
            select(LeadEvent).where(LeadEvent.source_type == "form_submission", LeadEvent.source_id == submission.id)
        )).scalars().first()
        if event is not None:
            if event.agent_viewed_at is None:
                event.agent_viewed_at = datetime.now(timezone.utc)
            lead = await db.get(Lead, event.lead_id)
            seen_up_to = event.occurred_at or submission.created_at
            if lead is not None and seen_up_to and (lead.viewed_at is None or lead.viewed_at < seen_up_to):
                lead.viewed_at = seen_up_to

    await db.commit()
    await db.refresh(submission)
    return submission


@router.delete("/submissions/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_submission(
    submission_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    result = await db.execute(
        select(FormSubmission).where(FormSubmission.id == submission_id, FormSubmission.owner_id == current_user.id)
    )
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    await db.delete(submission)
    await db.commit()


class ShowingConfirmUpdate(BaseModel):
    # Both None = undo (back to Pending for the visitor).
    confirmed_date: Optional[str] = None
    confirmed_time: Optional[str] = None


async def _showing_home(db: AsyncSession, submission: FormSubmission) -> dict:
    """The home a showing request is for - the MLS record when the visitor picked one."""
    from app.models.database import Property
    from app.services.agent_roundup_service import nice_street
    key = (submission.showing or {}).get("listing_key")
    prop = (await db.execute(select(Property).where(Property.listing_key == key).limit(1))).scalars().first() if key else None
    if prop is None:
        return {"address": submission.property_address or "", "street": (submission.property_address or "").split(",")[0]}
    photos = prop.photos if isinstance(prop.photos, list) else []
    return {
        "address": submission.property_address or prop.street_address or "",
        "street": nice_street(prop.street_address, prop.city) or (submission.property_address or "").split(",")[0],
        "city": (prop.city or "").title(), "state": prop.state or "", "zipcode": prop.zipcode or "",
        "photo": prop.img_src or (photos[0] if photos else None),
        "price": prop.price, "beds": prop.bedrooms, "baths": prop.bathrooms, "sqft": prop.living_area,
    }


async def _email_showing_confirmed(db: AsyncSession, submission: FormSubmission, form: AgentForm, agent: User) -> None:
    """Same "your tour is confirmed" email as a Showcase tour, linking back to the form page
    (which now shows the request as Confirmed). Never raises."""
    try:
        from app.services.agent_email_service import email_person
        from app.services.blacklist_service import BlacklistService
        from app.services.website.site_service import owner_links, resolve_owner_public_base_url
        to = submission.contact_email
        info = submission.showing or {}
        if not to or await BlacklistService.is_blacklisted(db, to):
            return
        try:
            when = datetime.strptime(f"{info.get('confirmed_date')} {info.get('confirmed_time')}", "%Y-%m-%d %H:%M")
            date_label = f"{when.strftime('%A, %B')} {when.day}"
            time_label = when.strftime("%I:%M %p").lstrip("0")
        except (TypeError, ValueError):
            date_label, time_label = info.get("confirmed_date") or "", info.get("confirmed_time") or ""
        home = await _showing_home(db, submission)
        hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == agent.id))).scalars().first()
        link = (await owner_links(db, agent, hub.slug)).form(form.slug) if hub and hub.slug else await resolve_owner_public_base_url(db, agent)
        agent_name = f"{agent.first_name or ''} {agent.last_name or ''}".strip()
        first = (submission.contact_name or "").strip().split(" ")[0]
        from app.services.calendar_invite_service import build_invite, eastern_start
        start = eastern_start(info.get("confirmed_date"), info.get("confirmed_time"))
        full = ", ".join(x for x in (home.get("address") or home["street"], home.get("city"), " ".join(y for y in (home.get("state"), home.get("zipcode")) if y)) if x)
        ics = build_invite(uid=f"showing-{submission.id}", start=start, street=home["street"], address=full, agent_name=agent_name,
                           agent_email=agent.email, agent_phone=await agent_public_phone(db, agent), client_name=submission.contact_name,
                           client_email=to, link=link) if start else None
        email_person(to, "tour_confirmed", f"Your tour of {home['street']} is confirmed - {date_label} at {time_label}", {
            "_ics": ics,
            "recipient_name": first[:1].upper() + first[1:] if first else "there",
            "street_address": home["street"], "city": home.get("city", ""), "state": home.get("state", ""),
            "image": home.get("photo") or "", "price": f"${home['price']:,.0f}" if home.get("price") else "",
            "date_label": date_label, "time_label": time_label,
            "agent_name": agent_name, "agent_email": agent.email or "", "agent_phone": await agent_public_phone(db, agent),
            "collection_link": link,
            **(await agent_email_vars(db, agent)),   # face, title, brokerage
        }, reply_to=agent.email, from_name=agent_name or "OpenHousePal", owner_id=agent.id)
    except Exception:  # noqa: BLE001 - an email must never break confirming the showing
        logger.warning("showing confirmed email failed", exc_info=True)


@router.put("/submissions/{submission_id}/showing", response_model=FormSubmissionListItem)
async def confirm_showing_submission(
    submission_id: str,
    payload: ShowingConfirmUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    """Schedule a Showing: the agent confirms one time (the visitor's link flips from Pending to
    Confirmed and they're emailed), or clears it again."""
    submission = (await db.execute(
        select(FormSubmission).where(FormSubmission.id == submission_id, FormSubmission.owner_id == current_user.id)
    )).scalar_one_or_none()
    if not submission or submission.form_type != "showing":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showing request not found")
    confirming = bool(payload.confirmed_date and payload.confirmed_time)
    if confirming:
        try:
            datetime.strptime(f"{payload.confirmed_date} {payload.confirmed_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick a valid date and time.")
    info = dict(submission.showing or {})
    info["confirmed_date"] = payload.confirmed_date if confirming else None
    info["confirmed_time"] = payload.confirmed_time if confirming else None
    submission.showing = info
    if confirming:
        submission.is_reviewed = True
    await db.commit()
    await db.refresh(submission)
    if confirming:
        form = await db.get(AgentForm, submission.form_id)
        if form is not None:
            await _email_showing_confirmed(db, submission, form, current_user)
    # The confirmed showing on the agent's own calendar (off it again when cleared) - Showcase
    # tours already did this; Agent Hub showings didn't.
    from app.services import calendar_sync_service
    calendar_sync_service.sync_showing_bg(current_user.id, submission.id)
    return submission


@router.put("/{form_id}", response_model=AgentFormResponse)
async def update_form(
    form_id: str,
    payload: AgentFormUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_active_plan),
):
    form = await get_owned_form(db, form_id, current_user.id)
    aesthetics_only = form.form_type in AESTHETICS_ONLY_FORM_TYPES

    if payload.questions is not None and not aesthetics_only:
        locked_ids = {question["id"] for question in form.questions if question.get("locked")}
        new_ids = {question.id for question in payload.questions}
        if not locked_ids.issubset(new_ids):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Core contact questions cannot be removed.")
        form.questions = [question.model_dump() for question in payload.questions]

    if payload.title is not None and not aesthetics_only:
        form.title = payload.title
    if payload.headline is not None and not aesthetics_only:
        form.headline = payload.headline
    if payload.subtitle is not None and not aesthetics_only:
        form.subtitle = payload.subtitle
    if payload.description is not None and not aesthetics_only:
        form.description = payload.description
    if payload.success_heading is not None and not aesthetics_only:
        form.success_heading = payload.success_heading
    if payload.success_message is not None and not aesthetics_only:
        form.success_message = payload.success_message
    if payload.accent_color is not None:
        form.accent_color = payload.accent_color
    if payload.show_branding is not None:
        form.show_branding = payload.show_branding
    if payload.dark_mode is not None:
        form.dark_mode = payload.dark_mode
    if payload.redirect_to_agent_hub is not None and not aesthetics_only:
        form.redirect_to_agent_hub = payload.redirect_to_agent_hub
    if payload.property_photo_carousel is not None:
        form.property_photo_carousel = payload.property_photo_carousel
    if payload.background_effects is not None:
        form.background_effects = payload.background_effects
    if payload.welcome_text_effects is not None:
        form.welcome_text_effects = payload.welcome_text_effects
    if payload.branding_layout is not None:
        form.branding_layout = payload.branding_layout
    if "linked_property" in payload.model_fields_set and form.form_type not in PROPERTY_LINK_DISABLED_TYPES and not aesthetics_only:
        form.linked_property = payload.linked_property.model_dump() if payload.linked_property else None

    await db.commit()
    await db.refresh(form)
    return form


def _agent_hub_is_live(agent: User, hub: AgentHub) -> bool:
    from app.utils.auth import owner_has_access, AGENT_HUB_ACCESS_TIERS
    from app.api.agent_hub_routes import _hub_has_minimum_content
    return owner_has_access(agent, AGENT_HUB_ACCESS_TIERS) and _hub_has_minimum_content(hub.config or {})


@router.get("/public/{agent_slug}/agent-busy")
async def public_agent_busy(agent_slug: str, days: int = 8, db: AsyncSession = Depends(get_db)):
    """The agent's busy blocks (start/end only) so the client's time picker only offers times the
    agent is actually free (user, 10/18). [] when no calendar is connected."""
    from app.services import calendar_sync_service
    hub = (await db.execute(select(AgentHub).where(AgentHub.slug == normalize_form_slug(agent_slug)))).scalar_one_or_none()
    if not hub:
        return {"busy": []}
    return {"busy": await calendar_sync_service.public_busy(db, hub.owner_id, days)}


@router.get("/public/{agent_slug}", response_model=List[PublicFormSummary])
async def list_public_forms(
    agent_slug: str,
    db: AsyncSession = Depends(get_db),
):
    hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_form_slug(agent_slug)))
    hub = hub_result.scalar_one_or_none()
    if not hub:
        return []

    forms_result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == hub.owner_id,
            AgentForm.is_enabled.is_(True),
        ).order_by(AgentForm.order_index)
    )
    forms = forms_result.scalars().all()
    return [
        PublicFormSummary(form_id=form.id, form_type=form.form_type, slug=form.slug, title=form.title)
        for form in forms
    ]


@router.get("/public/{agent_slug}/{form_slug}", response_model=PublicAgentFormResponse)
async def get_public_form(
    agent_slug: str,
    form_slug: str,
    c: Optional[str] = Query(None, description="Campaign token from the link (?c=) - its after-submit choice overrides the form's."),
    db: AsyncSession = Depends(get_db),
):
    hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_form_slug(agent_slug)))
    hub = hub_result.scalar_one_or_none()
    if not hub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")

    form_result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == hub.owner_id,
            AgentForm.slug == normalize_form_slug(form_slug),
            AgentForm.is_enabled.is_(True),
        )
    )
    form = form_result.scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")

    agent_result = await db.execute(select(User).where(User.id == hub.owner_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")
    from app.services.forms_service import assert_form_is_live
    await assert_form_is_live(db, form)

    # Submissions count regardless of whether the visitor is logged in, so views need to match -
    # excluding logged-in viewers here (but not on submit) made Completion Rate nonsensical
    # whenever an agent tested their own form while signed in: fills went up, views stayed at 0.
    try:
        db.add(AgentHubEvent(owner_id=form.owner_id, event_type="form_view"))
        await db.commit()
    except Exception as e:
        logger.warning("recording form view failed", extra={"error": str(e)})

    example_properties = None
    if form.form_type == "showcase_request":
        example_properties = _get_example_properties_for_showcase(hub.config or {})

    hub_live = _agent_hub_is_live(agent, hub)
    campaign_redirect, after_submit_mode, after_submit_url = campaign_after_submit(await active_campaign_for_token(db, form.id, c), hub_live)

    return PublicAgentFormResponse(
        form_id=form.id,
        form_type=form.form_type,
        slug=form.slug,
        title=form.title,
        headline=form.headline,
        subtitle=form.subtitle,
        questions=form.questions,
        success_heading=form.success_heading,
        success_message=form.success_message,
        accent_color=form.accent_color,
        show_branding=form.show_branding,
        dark_mode=form.dark_mode,
        # Only redirect to the Agent Hub when that page actually loads - on plans without Agent
        # Hub (Basic, No-MLS Basic) or before the agent has set it up, it would 404 right after the
        # guest submits. Same rule as agent_hub_routes._resolve_public_hub.
        redirect_to_agent_hub=campaign_redirect if campaign_redirect is not None else bool(form.redirect_to_agent_hub and hub_live),
        after_submit_mode=after_submit_mode,
        after_submit_url=after_submit_url,
        property_photo_carousel=form.property_photo_carousel,
        background_effects=getattr(form, "background_effects", True),
        welcome_text_effects=getattr(form, "welcome_text_effects", True),
        branding_layout=form.branding_layout,
        agent_name=f"{agent.first_name} {agent.last_name}".strip(),
        agent_brokerage=agent.brokerage,
        agent_photo_url=agent.photo_url,
        agent_title=agent.professional_title,
        agent_market=agent.market,
        linked_property=form.linked_property,
        example_properties=example_properties,
    )


@router.post("/public/{agent_slug}/{form_slug}/progress")
async def track_form_progress(
    agent_slug: str,
    form_slug: str,
    payload: FormProgressUpdate,
    db: AsyncSession = Depends(get_db),
    viewer: Optional[User] = Depends(get_current_user_optional),
):
    """Best-effort per-question progress ping (view / step-advance / completion) that powers the
    Stats drop-off funnel - never raises, since a tracking failure must never surface to the
    visitor filling out the form."""
    try:
        hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_form_slug(agent_slug)))
        hub = hub_result.scalar_one_or_none()
        if not hub:
            return {"success": True}

        form_result = await db.execute(
            select(AgentForm).where(
                AgentForm.owner_id == hub.owner_id,
                AgentForm.slug == normalize_form_slug(form_slug),
            )
        )
        form = form_result.scalar_one_or_none()
        if not form:
            return {"success": True}
        # The agent trying out their own form isn't a visitor - same rule as Agent Hub visits and
        # blog reads, so "Forms Viewed" on Dash only counts real guests.
        if viewer and viewer.id == form.owner_id:
            return {"success": True}

        await record_form_progress(db, form, payload)
        return {"success": True}
    except Exception as e:
        logger.warning("tracking form progress failed", extra={"error": str(e)})
        try:
            await db.rollback()
        except Exception:
            pass
        return {"success": True}


@router.post("/public/{agent_slug}/{form_slug}/submit", response_model=FormSubmissionResponse)
async def submit_public_form(
    agent_slug: str,
    form_slug: str,
    payload: FormSubmissionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.services.public_rate_limit import check_public_submit
    check_public_submit(request)
    hub_result = await db.execute(select(AgentHub).where(AgentHub.slug == normalize_form_slug(agent_slug)))
    hub = hub_result.scalar_one_or_none()
    if not hub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")

    form_result = await db.execute(
        select(AgentForm).where(
            AgentForm.owner_id == hub.owner_id,
            AgentForm.slug == normalize_form_slug(form_slug),
            AgentForm.is_enabled.is_(True),
        )
    )
    form = form_result.scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This form could not be found.")

    # Actual validation/persistence/lead-upsert logic lives in forms_service.submit_form, shared
    # with the website's site-scoped form endpoint (public_site_routes.py) so both public entry
    # points submit into the exact same pipeline instead of duplicating it.
    from app.services.forms_service import submit_form
    return await submit_form(db, form, payload)


@router.get("/public/{agent_slug}/{form_slug}/showing/{submission_id}")
async def get_public_showing_status(
    agent_slug: str,
    form_slug: str,
    submission_id: str,
    db: AsyncSession = Depends(get_db),
):
    """A visitor coming back to their Schedule a Showing link: their request (remembered on their
    device by submission id) as Pending or Confirmed - the same status the Showcase shows."""
    hub = (await db.execute(select(AgentHub).where(AgentHub.slug == normalize_form_slug(agent_slug)))).scalar_one_or_none()
    form = (await db.execute(
        select(AgentForm).where(AgentForm.owner_id == hub.owner_id, AgentForm.slug == normalize_form_slug(form_slug))
    )).scalar_one_or_none() if hub else None
    submission = await db.get(FormSubmission, submission_id) if form else None
    if not submission or submission.form_id != form.id or submission.showing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Showing request not found")
    info = submission.showing or {}
    preferred = info.get("preferred") or []
    slot = lambda i, k: preferred[i].get(k) if len(preferred) > i else None  # noqa: E731
    return {
        "id": submission.id,
        "home": await _showing_home(db, submission),
        "contact_name": submission.contact_name,
        "preferred_date": slot(0, "date"), "preferred_time": slot(0, "time"),
        "preferred_date_2": slot(1, "date"), "preferred_time_2": slot(1, "time"),
        "preferred_date_3": slot(2, "date"), "preferred_time_3": slot(2, "time"),
        "confirmed_date": info.get("confirmed_date"), "confirmed_time": info.get("confirmed_time"),
        "message": info.get("note"),
        "created_at": submission.created_at.isoformat() if submission.created_at else None,
    }
