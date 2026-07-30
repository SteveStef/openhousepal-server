import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import AgentForm, AgentHub, FormSubmission, Notification, User
from app.schemas.forms import (
    AgentFormCreate,
    AgentFormLayoutUpdate,
    AgentFormResponse,
    AgentFormUpdate,
    FormSubmissionCreate,
    FormSubmissionListItem,
    FormSubmissionResponse,
    PublicAgentFormResponse,
    PublicFormSummary,
    SlugCheckResponse,
    SlugUpdate,
    SubmissionReviewedUpdate,
)
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/agent-hub/forms", tags=["forms"])
logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_FORM_SLUGS = {"sales"}
BRAND_ACCENT = "#816937"  # RGB(129,105,55) - default form accent color
PROPERTY_LINK_DISABLED_TYPES = {"buying", "selling", "home_value", "seller_onboarding", "buyer_onboarding", "how_can_i_help", "showcase_request"}  # only true custom/manual forms support property-linking

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
             "options": ["ASAP", "1–3 months", "3–6 months", "6–12 months", "Just researching"]},
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
            {"id": "property_address", "type": "short_text", "label": "Property address", "required": True, "locked": True},
            {"id": "price_expectation", "type": "short_text", "label": "What price are you looking for?", "required": False, "locked": False},
            {"id": "move_reason", "type": "short_text", "label": "What's prompting the move?", "required": False, "locked": False},
            {"id": "timeline", "type": "select", "label": "When are you hoping to sell?", "required": False, "locked": False,
             "options": ["ASAP", "1–3 months", "3–6 months", "6–12+ months", "Just curious"]},
            {"id": "notes", "type": "long_text", "label": "Anything else?", "required": False, "locked": False},
        ],
    },
    "home_value": {
        "slug": "home-value",
        "title": "What's My Home Worth?",
        "description": "Collect a homeowner's property information so you can prepare a home-value estimate.",
        "headline": "Let's Find Out What Your Home Is Worth",
        "subtitle": "Get a quick market valuation for your property.",
        "success_heading": "Thanks!",
        "success_message": "I'll put together your home value estimate and follow up soon.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "property_address", "type": "short_text", "label": "Property address", "required": True, "locked": True},
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
            {"id": "sell_timeline", "type": "select", "label": "When are you thinking of selling?", "required": False, "locked": False,
             "options": ["ASAP", "1–3 months", "3–6 months", "6–12+ months", "Not sure yet"]},
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
        "title": "How Can I Help?",
        "description": "A general lead form for all leads - typically used in bios, YouTube descriptions, etc.",
        "headline": "How Can I Help?",
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
        "title": "Request a Showcase",
        "description": "For open house guests who want their own personalized property showcase - reached via the QR code on your Brag Sheet handout.",
        "headline": "Get Your Own Personalized Showcase",
        "subtitle": "Tell me a bit about what you're looking for and I'll build you a live, personalized collection of matching homes.",
        "success_heading": "You're all set!",
        "success_message": "I'll put together your showcase and send it your way shortly.",
        "accent_color": BRAND_ACCENT,
        "questions": [
            {"id": "contact_info", "type": "contact_group", "label": "Your Contact Information", "required": True, "locked": True,
             "fields": [
                 {"id": "full_name", "type": "short_text", "label": "Full name", "required": True},
                 {"id": "email", "type": "email", "label": "Email", "required": True},
                 {"id": "phone", "type": "phone", "label": "Phone", "required": True},
             ]},
            {"id": "desired_area", "type": "short_text", "label": "What towns, neighborhoods, or school districts are you interested in?", "required": False, "locked": False},
            {"id": "price_range", "type": "contact_group", "label": "Price Range", "required": False, "locked": False,
             "fields": [
                 {"id": "budget_min", "type": "currency", "label": "Minimum price", "required": False},
                 {"id": "budget_max", "type": "currency", "label": "Maximum price", "required": False},
             ]},
            {"id": "beds_baths", "type": "contact_group", "label": "How many bedrooms and bathrooms do you need?", "required": False, "locked": False,
             "fields": [
                 {"id": "min_bedrooms", "type": "number", "label": "Minimum bedrooms", "required": False},
                 {"id": "min_bathrooms", "type": "number", "label": "Minimum bathrooms", "required": False},
             ]},
            {"id": "home_type", "type": "select", "label": "What type of property are you looking for?", "required": False, "locked": False,
             "options": ["Single-family home", "Condo/Townhouse", "Multi-family", "Land", "Other"]},
            {"id": "notes", "type": "long_text", "label": "Anything else we should know?", "required": False, "locked": False},
        ],
    },
}


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

    return sorted(all_forms, key=lambda form: form.order_index)


async def get_owned_form(db: AsyncSession, form_id: str, owner_id: str) -> AgentForm:
    result = await db.execute(select(AgentForm).where(AgentForm.id == form_id, AgentForm.owner_id == owner_id))
    form = result.scalar_one_or_none()
    if not form:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Form not found")
    return form


@router.get("/me", response_model=List[AgentFormResponse])
async def get_my_forms(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    return await get_or_create_forms(db, current_user.id)


@router.post("", response_model=AgentFormResponse, status_code=status.HTTP_201_CREATED)
async def create_form(
    payload: AgentFormCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
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
    current_user: User = Depends(get_current_active_user),
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
    current_user: User = Depends(get_current_active_user),
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


@router.get("/{form_id}/slug/check", response_model=SlugCheckResponse)
async def check_form_slug(
    form_id: str,
    slug: str = Query(..., min_length=1, max_length=80),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
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
    current_user: User = Depends(get_current_active_user),
):
    form = await get_owned_form(db, form_id, current_user.id)
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


@router.get("/{form_id}/submissions", response_model=List[FormSubmissionListItem])
async def get_form_submissions(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    await get_owned_form(db, form_id, current_user.id)
    result = await db.execute(
        select(FormSubmission)
        .where(FormSubmission.form_id == form_id, FormSubmission.owner_id == current_user.id)
        .order_by(FormSubmission.created_at.desc())
    )
    return result.scalars().all()


@router.patch("/submissions/{submission_id}/reviewed", response_model=FormSubmissionListItem)
async def mark_submission_reviewed(
    submission_id: str,
    payload: SubmissionReviewedUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(
        select(FormSubmission).where(FormSubmission.id == submission_id, FormSubmission.owner_id == current_user.id)
    )
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    submission.is_reviewed = payload.is_reviewed
    await db.commit()
    await db.refresh(submission)
    return submission


@router.delete("/submissions/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_submission(
    submission_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(
        select(FormSubmission).where(FormSubmission.id == submission_id, FormSubmission.owner_id == current_user.id)
    )
    submission = result.scalar_one_or_none()
    if not submission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")

    await db.delete(submission)
    await db.commit()


@router.put("/{form_id}", response_model=AgentFormResponse)
async def update_form(
    form_id: str,
    payload: AgentFormUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    form = await get_owned_form(db, form_id, current_user.id)

    if payload.questions is not None:
        locked_ids = {question["id"] for question in form.questions if question.get("locked")}
        new_ids = {question.id for question in payload.questions}
        if not locked_ids.issubset(new_ids):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Core contact questions cannot be removed.")
        form.questions = [question.model_dump() for question in payload.questions]

    if payload.title is not None:
        form.title = payload.title
    if payload.headline is not None:
        form.headline = payload.headline
    if payload.subtitle is not None:
        form.subtitle = payload.subtitle
    if payload.description is not None:
        form.description = payload.description
    if payload.success_heading is not None:
        form.success_heading = payload.success_heading
    if payload.success_message is not None:
        form.success_message = payload.success_message
    if payload.accent_color is not None:
        form.accent_color = payload.accent_color
    if payload.show_branding is not None:
        form.show_branding = payload.show_branding
    if payload.dark_mode is not None:
        form.dark_mode = payload.dark_mode
    if payload.redirect_to_agent_hub is not None:
        form.redirect_to_agent_hub = payload.redirect_to_agent_hub
    if payload.property_photo_carousel is not None:
        form.property_photo_carousel = payload.property_photo_carousel
    if payload.branding_layout is not None:
        form.branding_layout = payload.branding_layout
    if "linked_property" in payload.model_fields_set and form.form_type not in PROPERTY_LINK_DISABLED_TYPES:
        form.linked_property = payload.linked_property.model_dump() if payload.linked_property else None

    await db.commit()
    await db.refresh(form)
    return form


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
        redirect_to_agent_hub=form.redirect_to_agent_hub,
        property_photo_carousel=form.property_photo_carousel,
        branding_layout=form.branding_layout,
        agent_name=f"{agent.first_name} {agent.last_name}".strip(),
        agent_brokerage=agent.brokerage,
        agent_photo_url=agent.photo_url,
        agent_title=agent.professional_title,
        agent_market=agent.market,
        linked_property=form.linked_property,
    )


@router.post("/public/{agent_slug}/{form_slug}/submit", response_model=FormSubmissionResponse)
async def submit_public_form(
    agent_slug: str,
    form_slug: str,
    payload: FormSubmissionCreate,
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

    answers_by_id = {answer.question_id: answer.value for answer in payload.answers}

    def is_visible(question: dict) -> bool:
        depends_on = question.get("depends_on")
        if not depends_on:
            return True
        dep_value = answers_by_id.get(depends_on["question_id"])
        if dep_value is None:
            return False
        values = depends_on.get("values", [])
        if isinstance(dep_value, list):
            return any(item in values for item in dep_value)
        return dep_value in values

    def is_blank(value) -> bool:
        if value is None:
            return True
        if isinstance(value, list):
            return len(value) == 0
        return str(value).strip() == ""

    snapshot = []
    for question in form.questions:
        if not is_visible(question):
            continue
        if question.get("type") == "statement":
            continue
        if question.get("type") == "contact_group":
            group_title = question.get("section_title") or question.get("label")
            for field in question.get("fields", []):
                value = answers_by_id.get(field["id"])
                if field.get("required") and is_blank(value):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field['label']} is required.")
                snapshot.append({
                    "questionId": field["id"],
                    "label": field["label"],
                    "type": field["type"],
                    "value": value,
                    "sectionTitle": group_title,
                })
            continue
        value = answers_by_id.get(question["id"])
        if question.get("required") and is_blank(value):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{question['label']} is required.")
        snapshot.append({
            "questionId": question["id"],
            "label": question["label"],
            "type": question["type"],
            "value": value,
            "sectionTitle": question.get("section_title"),
        })

    def contact_value(*question_ids: str):
        for question_id in question_ids:
            value = answers_by_id.get(question_id)
            if value not in (None, "") and not isinstance(value, list):
                return str(value).strip()
        return None

    def contact_name_value():
        for first_id, last_id in (("buyer_first_name", "buyer_last_name"), ("buyer1_first_name", "buyer1_last_name")):
            first = answers_by_id.get(first_id)
            last = answers_by_id.get(last_id)
            combined = f"{first or ''} {last or ''}".strip()
            if combined:
                return combined
        return contact_value("full_name", "seller_name", "seller1_name")

    submission = FormSubmission(
        form_id=form.id,
        owner_id=form.owner_id,
        form_type=form.form_type,
        answers=snapshot,
        contact_name=contact_name_value(),
        contact_email=contact_value("email", "seller_email", "seller1_email", "buyer_email", "buyer1_email"),
        contact_phone=contact_value("phone", "seller_phone", "seller1_phone", "buyer_phone", "buyer1_phone"),
        property_address=contact_value("property_address", "address_line1"),
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)

    try:
        notification = Notification(
            agent_id=form.owner_id,
            type="FORM_SUBMISSION",
            reference_type="FORM_SUBMISSION",
            reference_id=submission.id,
            title=f"New {form.title} submission",
            message=f"{submission.contact_name or 'Someone'} submitted your {form.title} form",
            visitor_name=submission.contact_name,
            property_address=submission.property_address,
            link=f"/agent-hub?tab=Forms Results&formId={form.id}",
            is_read=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(notification)
        await db.commit()
    except Exception as e:
        logger.error("creating form submission notification failed", extra={"error": str(e)})

    return FormSubmissionResponse(
        success=True,
        message=form.success_message or "Thanks! I'll be in touch soon.",
        submission_id=submission.id,
    )
