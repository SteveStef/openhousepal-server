from app.services.areas import area_enrichment_service, area_research_service


def resolved_place(notes: str = "searched and found relevant options") -> dict:
    return {
        "entities": [{
            "name": "Example",
            "relationship": "IN_AREA",
            "source_url": "https://example.com",
            "confidence": 0.9,
        }],
        "notes": notes,
    }


def complete_facts() -> dict:
    facts = {category: resolved_place() for category in area_research_service.RESEARCH_CATEGORIES}
    facts["public_schools"] = {
        "districts": [{"name": "Example District", "source_url": "https://example.com", "confidence": 0.9}],
        "crosses_district_boundary": False,
        "assignment_depends_on_exact_address": False,
        "elementary_schools": [],
        "middle_schools": [],
        "high_schools": [],
    }
    facts["market_context"] = {
        "stats": [{
            "label": "Median sale price",
            "value": "$500,000",
            "geography": "Example, PA",
            "time_period": "2026",
            "source_url": "https://example.com",
            "confidence": 0.9,
        }],
        "notes": "searched current market data",
    }
    return facts


def test_complete_required_research_is_enriched_even_if_optional_sources_failed():
    facts = complete_facts()

    assert area_enrichment_service.enrichment_status_for_facts(facts) == "ENRICHED"


def test_missing_private_schools_without_none_found_note_is_partial():
    facts = complete_facts()
    facts["private_schools"] = {"entities": []}

    assert area_enrichment_service.unresolved_research_categories(facts) == ["private_schools"]
    assert area_enrichment_service.enrichment_status_for_facts(facts) == "PARTIAL"


def test_private_schools_empty_result_requires_and_accepts_researched_note():
    assert "notes" in area_research_service._PRIVATE_SCHOOLS_TOOL_SCHEMA["required"]

    cleaned, _dropped = area_research_service.validate_category_result(
        "private_schools",
        {
            "entities": [],
            "notes": "Searched private schools in and near this community and found none applicable.",
        },
    )

    assert area_research_service.category_is_resolved("private_schools", cleaned)
