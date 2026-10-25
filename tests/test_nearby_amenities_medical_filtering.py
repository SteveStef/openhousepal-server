import pytest

from app.services import nearby_amenities_service as svc


def place(name, types, lat=40.0, lng=-75.0):
    return {
        "displayName": {"text": name},
        "primaryType": types[0] if types else None,
        "types": types,
        "formattedAddress": f"1 Main St, Test City, PA",
        "location": {"latitude": lat, "longitude": lng},
    }


def test_rejects_outpatient_and_primary_care_hospital_results():
    assert not svc._is_legitimate_full_service_hospital(place("Reading Hospital Outpatient Therapy", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("ChristianaCare Primary Care at Hockessin", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Main Line Health Imaging Center", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Ambulatory Surgery Center", ["hospital"]))


def test_accepts_full_service_hospital_names():
    assert svc._is_legitimate_full_service_hospital(place("Reading Hospital", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Paoli Hospital", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Jefferson Einstein Medical Center", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Regional Medical Center", ["medical_center"]))


def test_rejects_medical_office_buildings_misclassified_as_hospitals():
    """A real Google Places result named "Numors mob Milford" (primaryType=hospital, garbled
    "Nemours" branding, no other red-flag keyword) turned out to be "Nemours duPont Pediatrics,
    Milford" (confirmed via web search) - an outpatient pediatric office sharing an address with
    the real adjacent hospital (Bayhealth Hospital, Sussex Campus), which is why the nearest-first
    selection picked the wrong one. "mob" must match as a whole word only, never a bare substring,
    so real place names containing "mobile"/"automobile" are unaffected."""
    assert not svc._is_legitimate_full_service_hospital(place("Numors mob Milford", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Regional MOB", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Hospital Medical Office Building 2", ["hospital"]))
    # word-boundary safety - must not false-positive on words merely containing "mob"
    assert svc._is_legitimate_full_service_hospital(place("Mobile County Hospital", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Automobile Row Hospital", ["hospital"]))


def test_rejects_institute_branded_non_hospital_facilities():
    """A real Google Places API result named just "Argo institute" (primaryType=hospital, no
    other red-flag types/keywords) turned out to be a substance-use treatment center (Aquila
    Behavioral Health), not a hospital - Google's own listing gives no other lexical hint of
    that. None of the genuine hospitals confirmed in testing (Bryn Mawr Hospital, Hospital of the
    University of Pennsylvania, Thomas Jefferson University Hospital, Lankenau Medical Center,
    Mercy Fitzgerald Hospital, Cooper University Hospital, etc.) use "Institute" in their
    Google-displayed name, so this exclusion has no observed false-negative cost."""
    assert not svc._is_legitimate_full_service_hospital(place("Argo institute", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Wellness Institute", ["hospital"]))
    # real hospitals must still pass
    assert svc._is_legitimate_full_service_hospital(place("Bryn Mawr Hospital", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Hospital of the University of Pennsylvania", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Thomas Jefferson University Hospital", ["hospital"]))


def test_rejects_individual_practitioner_names():
    """Real Google Places results named "Jessica A. Quirin, CRNP" and "Bryn A. Graham, PA-C"
    (primaryType=medical_clinic, surfaced by the "urgent care" text search despite having nothing
    to do with urgent care) turned out to be individual providers' solo listings, not a patient-care
    facility - Google's own displayName gives no other lexical hint of that. A Cheat Sheet medical
    result must be a named facility, not a person."""
    assert svc._non_hospital_medical_label(place("Jessica A. Quirin, CRNP", ["medical_clinic"])) is None
    assert svc._non_hospital_medical_label(place("Bryn A. Graham, PA-C", ["medical_clinic"])) is None
    assert svc._non_hospital_medical_label(place("John R. Smith, NP", ["doctor"])) is None
    assert svc._non_hospital_medical_label(place("Maria Gonzalez, DNP", ["medical_clinic"])) is None
    # real facility names must still pass, including ones with commas
    assert svc._non_hospital_medical_label(place("Lancaster General Health Downtown Pavilion", ["medical_clinic"])) == "MEDICAL CLINIC"
    assert svc._non_hospital_medical_label(place("Family Medical Centre, P.A.", ["medical_center"])) == "MEDICAL CENTER"
    # "MD" is deliberately excluded from the credential list - it collides with the Maryland state
    # abbreviation, which can legitimately trail a real name/address.
    assert not svc._is_individual_provider_name("Some Clinic, MD")


def test_rejects_bare_street_address_names():
    """A real Google Places result had primaryType=hospital but Google returned a bare street
    address as the business name - "93 Church St Frederica, DE" - instead of a real facility name.
    The actual nearest real hospital (Bayhealth Hospital, Kent Campus) was ~3mi farther away and
    lost out purely because the address-named ghost listing was closer."""
    assert not svc._is_legitimate_full_service_hospital(place("93 Church St Frederica, DE", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("100 Wellness Way, Milford, DE", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Bayhealth Hospital, Kent Campus", ["hospital"]))


def test_rejects_ghost_and_administrative_hospital_listings():
    """primaryType=hospital candidates found in real testing that are not an actual hospital
    building: "Myblood" (a blood-donation service), a bare "Delaware", "Bayhealth Purchasing
    Department, Dover" (an admin/supply office), and a bare "Bayhealth" (665 S Bay Rd - a different
    address than the real "Bayhealth Hospital, Kent Campus" building at 640 S State St). A hospital
    result must say so explicitly (Hospital/Medical Center/Campus), not just carry a health-system
    brand name with no further qualifier."""
    assert not svc._is_legitimate_full_service_hospital(place("Myblood", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Delaware", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Bayhealth", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Bayhealth Purchasing Department, Dover", ["hospital"]))
    # real hospitals must still pass
    assert svc._is_legitimate_full_service_hospital(place("Bayhealth Hospital, Kent Campus", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Reading Hospital", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Jefferson Einstein Medical Center", ["hospital"]))


def test_specialist_cancer_institute_ranked_below_general_facility():
    """"Ann B. Barshinger Cancer Institute" is a real oncology facility (primaryType=medical_clinic)
    that doesn't literally say "oncology" - found outranking closer general-purpose clinics once the
    individual-practitioner false positives that used to mask this were fixed."""
    cancer_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("Ann B. Barshinger Cancer Institute", ["medical_clinic"]))
    general_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("Lancaster General Health Downtown Pavilion", ["medical_clinic"]))
    assert cancer_rank > general_rank


def test_rejects_hospital_department_and_specialty_offices():
    """A real Google result, "Union Hospital Cecil County Pulmn" (primaryType=hospital, a
    truncated "Pulmonary"/"Pulmonology" department suffix), was 0.18mi closer than the actual
    hospital campus ("ChristianaCare Union Hospital") and won purely on that margin - it has none
    of the other red-flag keywords. Deliberately does NOT reuse SPECIALIST_MEDICAL_NAME_KEYWORDS
    wholesale here - that list includes "surgery"/"surgical", which appear in real, correct
    hospital-campus listings (e.g. "Bayhealth General Surgery, Sussex Campus")."""
    assert not svc._is_legitimate_full_service_hospital(place("Union Hospital Cecil County Pulmn", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("ChristianaCare Audiology at Elkton", ["hospital"]))
    assert not svc._is_legitimate_full_service_hospital(place("Union Specialty Associates OV/GYN", ["hospital"]))
    # real hospitals, including one with "Surgery" literally in its campus-listing name, must pass
    assert svc._is_legitimate_full_service_hospital(place("ChristianaCare Union Hospital", ["hospital"]))
    assert svc._is_legitimate_full_service_hospital(place("Bayhealth General Surgery, Sussex Campus", ["hospital"]))


def test_demotes_eye_care_brands_and_occupational_medicine():
    """Real results "MyEyeDr." (a vision/eyewear retail chain, no space so the word-boundary "eye"
    check in SPECIALIST_MEDICAL_NAME_KEYWORDS never matches it) and "CNS Occupational Medicine of
    Delaware" (treats workplace injuries, not general patient care) were winning general-care slots
    in sparse markets. Demoted, not fully excluded - still usable as a last resort."""
    general_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("Some General Clinic", ["medical_clinic"]))
    eye_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("MyEyeDr.", ["medical_clinic"]))
    occ_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("CNS Occupational Medicine of Delaware", ["medical_clinic"]))
    specialty_care_rank = svc._non_hospital_medical_rank("DOCTOR", place("Nemours Children's Health, Deptford - Specialty Care", ["doctor"]))
    assert eye_rank > general_rank
    assert occ_rank > general_rank
    assert specialty_care_rank > general_rank


def test_demotes_childrens_hospital_from_general_medical_slot():
    """A real result, "St. Christopher's Hospital for Children," has Google primaryType=medical_clinic
    (not hospital) for this Place entry, so it never hits the hospital-only exclusion in
    _non_hospital_medical_label - and "pediatric"/"pediatrics" don't match its literal name ("Children"
    isn't "pediatric"), so it was winning the adult/family "General Medical" slot outright at two
    independent real properties. Demoted, not fully excluded - still usable as a last resort in a
    market with no other general-care option."""
    general_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("Some Family Clinic", ["medical_clinic"]))
    childrens_rank = svc._non_hospital_medical_rank(
        "MEDICAL CLINIC", place("St. Christopher's Hospital for Children", ["medical_clinic"])
    )
    chop_rank = svc._non_hospital_medical_rank(
        "MEDICAL CENTER", place("Children's Hospital of Philadelphia - Specialty Care Center", ["medical_center"])
    )
    assert childrens_rank > general_rank
    assert chop_rank > general_rank


@pytest.mark.asyncio
async def test_fetch_medical_care_prefers_general_practice_over_childrens_hospital(monkeypatch):
    async def fake_search_nearby_types(*args, **kwargs):
        included_types = args[4] if len(args) > 4 else kwargs.get("included_types")
        if "hospital" in included_types or "general_hospital" in included_types:
            return [place("Temple University Hospital - Main Campus", ["hospital"], lat=40.0, lng=-75.03)]
        return [
            place("St. Christopher's Hospital for Children", ["medical_clinic"], lat=40.0, lng=-75.01),
            place("Jefferson Einstein Philadelphia Hospital", ["medical_center"], lat=40.0, lng=-75.02),
        ]

    async def fake_search_text(*args, **kwargs):
        return []

    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)
    monkeypatch.setattr(svc, "_search_text", fake_search_text)

    results = await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0)

    assert [r.name for r in results] == ["Temple University Hospital - Main Campus", "Jefferson Einstein Philadelphia Hospital"]


def test_general_patient_care_labels_are_ranked_by_proximity_not_label_order():
    """A real Lancaster PA result had a 5.4mi "MEDICAL CLINIC" beating a legitimate, closer 2.7mi
    "MEDICAL CENTER" purely because of label priority - FAMILY MEDICINE/MEDICAL CLINIC/DOCTOR/
    MEDICAL CENTER must now all rank equally (tier 0) so distance decides among them. URGENT CARE
    stays a strictly lower tier, unchanged."""
    clinic_rank = svc._non_hospital_medical_rank("MEDICAL CLINIC", place("Lancaster General Health Downtown Pavilion", ["medical_clinic"]))
    center_rank = svc._non_hospital_medical_rank("MEDICAL CENTER", place("Lancaster General Health Suburban Pavilion", ["medical_center"]))
    family_rank = svc._non_hospital_medical_rank("FAMILY MEDICINE", place("Some Family Practice", ["medical_clinic"]))
    doctor_rank = svc._non_hospital_medical_rank("DOCTOR", place("Some Doctor Office", ["doctor"]))
    urgent_rank = svc._non_hospital_medical_rank("URGENT CARE", place("Some Urgent Care", ["medical_clinic"]))
    assert clinic_rank == center_rank == family_rank == doctor_rank
    assert urgent_rank > clinic_rank


def test_walk_in_clinics_ranked_as_urgent_care_not_general_clinic():
    """Real Google results "Delaware Walk In Medical" and "Bayhealth Walk-in Medical Care" have no
    literal "urgent care" wording, so they were competing as ordinary general clinics once label-
    priority ranking no longer buried them behind a FAMILY MEDICINE/MEDICAL CLINIC result regardless
    of distance. Bare "walk-in"/"walk in" wording is the same kind of episodic, no-appointment care."""
    assert svc._non_hospital_medical_label(place("Delaware Walk In Medical", ["medical_clinic"])) == "URGENT CARE"
    assert svc._non_hospital_medical_label(place("Bayhealth Walk-in Medical Care, Blue Hen", ["medical_clinic"])) == "URGENT CARE"
    # a real family medicine practice must not be caught by this
    assert svc._non_hospital_medical_label(place("Dover Family Physicians PA", ["doctor"])) != "URGENT CARE"


def test_orthopaedics_spelling_variant_demoted_as_specialist():
    """A real result, "Rothman Orthopaedics" (British/medical spelling), isn't caught by the
    "orthopedic"/"orthopedics" (American spelling) keywords already in SPECIALIST_MEDICAL_NAME_KEYWORDS
    - found outranking a real general clinic once label-priority ranking no longer masked it."""
    ortho_rank = svc._non_hospital_medical_rank("DOCTOR", place("Rothman Orthopaedics", ["doctor"]))
    general_rank = svc._non_hospital_medical_rank("DOCTOR", place("Penn Medicine Radnor", ["doctor"]))
    assert ortho_rank > general_rank


@pytest.mark.asyncio
async def test_fetch_medical_care_prefers_closer_medical_center_over_farther_clinic(monkeypatch):
    """End-to-end regression for the real Lancaster PA case: today's rigid label order used to pick
    a farther MEDICAL CLINIC over a legitimate, closer MEDICAL CENTER. Also mixes in a walk-in
    clinic and an "Orthopaedics"-spelled specialist to prove both keyword-gap fixes hold together
    in the full pipeline, not just in isolated unit checks."""
    async def fake_search_text(*args, **kwargs):
        return [place("Premier Physical Therapy", ["physiotherapist"], lat=40.0, lng=-75.001)]  # no urgent care around

    async def fake_search_nearby_types(*args, **kwargs):
        included_types = _included_types(args, kwargs)
        if "hospital" in included_types:
            return []
        return [
            place("Downtown Medical Clinic", ["medical_clinic"], lat=40.0, lng=-75.08),
            place("Suburban Medical Center", ["medical_center"], lat=40.0, lng=-75.03),
            place("Rothman Orthopaedics", ["doctor"], lat=40.0, lng=-75.01),
        ]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0)

    names = [r.name for r in results]
    assert "Suburban Medical Center" in names
    assert "Downtown Medical Clinic" not in names
    assert "Rothman Orthopaedics" not in names


def test_urgent_care_must_be_actual_urgent_care():
    assert svc._is_legitimate_urgent_care(place("Tower Health Urgent Care - Wyomissing", ["health"]))
    assert svc._is_legitimate_urgent_care(place("Main Line Health Urgent Care", ["doctor"]))
    assert not svc._is_legitimate_urgent_care(place("Tower Health Primary Care", ["doctor"]))
    assert not svc._is_legitimate_urgent_care(place("Premier Physical Therapy", ["physiotherapist"]))
    assert not svc._is_legitimate_urgent_care(place("Quest Diagnostics Lab", ["doctor"]))


def test_nearest_valid_place_skips_bad_nearest_hospital():
    raw = [
        place("Reading Hospital Outpatient Therapy", ["hospital"], lat=40.0, lng=-75.001),
        place("Reading Hospital", ["hospital"], lat=40.0, lng=-75.03),
        place("Farther Real Hospital", ["hospital"], lat=40.0, lng=-75.08),
    ]
    result = svc._nearest_valid_place(raw, 40.0, -75.0, svc._is_legitimate_full_service_hospital, "HOSPITAL")
    assert result is not None
    assert result.name == "Reading Hospital"
    assert result.place_type == "HOSPITAL"


def _included_types(args, kwargs):
    """_search_nearby_types is now used for BOTH the hospital-types call and the non-hospital-
    types call (merged from 2 separate calls into 1 for hospitals - see _fetch_medical_care) -
    fakes need to discriminate on the actual `included_types` argument, not just on which function
    was patched."""
    return args[4] if len(args) > 4 else kwargs.get("included_types")


@pytest.mark.asyncio
async def test_fetch_medical_care_returns_nearest_hospital_and_non_hospital_provider(monkeypatch):
    async def fake_search_text(*args, **kwargs):
        return [
            place("Tower Health Primary Care", ["doctor"], lat=40.0, lng=-75.001),
            place("Tower Health Urgent Care - Wyomissing", ["health"], lat=40.0, lng=-75.01),
            place("Farther Urgent Care", ["health"], lat=40.0, lng=-75.04),
        ]

    async def fake_search_nearby_types(*args, **kwargs):
        included_types = _included_types(args, kwargs)
        if "hospital" in included_types:
            return [
                place("Reading Hospital Outpatient Therapy", ["hospital"], lat=40.0, lng=-75.001),
                place("Reading Hospital", ["hospital"], lat=40.0, lng=-75.02),
                place("Farther Hospital", ["hospital"], lat=40.0, lng=-75.05),
            ]
        return [
            place("Tower Health Family Medicine", ["doctor"], lat=40.0, lng=-75.005),
            place("Tower Health Medical Clinic", ["medical_clinic"], lat=40.0, lng=-75.003),
        ]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0)

    # 10/25: the second slot is the nearest urgent care - the doctor's offices only fill in when
    # there is none (see test_fetch_medical_care_prefers_closer_medical_center_over_farther_clinic).
    assert [(r.place_type, r.name) for r in results] == [
        ("HOSPITAL", "Reading Hospital"),
        ("URGENT CARE", "Tower Health Urgent Care - Wyomissing"),
    ]


@pytest.mark.asyncio
async def test_fetch_medical_care_prefers_real_facility_over_individual_provider(monkeypatch):
    """End-to-end regression for the real "Jessica A. Quirin, CRNP" case: an individual provider's
    solo listing shows up closer (via the urgent-care text search) than a real clinic facility, but
    the real facility must still win the non-hospital slot."""
    async def fake_search_text(*args, **kwargs):
        return [
            place("Jessica A. Quirin, CRNP", ["medical_clinic"], lat=40.0, lng=-75.002),
            place("Tower Health Urgent Care", ["health"], lat=40.0, lng=-75.04),
        ]

    async def fake_search_nearby_types(*args, **kwargs):
        included_types = _included_types(args, kwargs)
        if "hospital" in included_types:
            return [place("Reading Hospital", ["hospital"], lat=40.0, lng=-75.02)]
        return [place("Tower Health Medical Clinic", ["medical_clinic"], lat=40.0, lng=-75.01)]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0)

    names = [r.name for r in results]
    assert "Jessica A. Quirin, CRNP" not in names
    assert "Tower Health Urgent Care" in names


@pytest.mark.asyncio
async def test_fetch_medical_care_hospital_search_is_one_combined_call_not_two(monkeypatch):
    """Regression: general_hospital + hospital must be searched together in ONE
    _search_nearby_types call per radius tier, not as two separate _search_nearby calls (the
    pre-optimization shape). Fixture finds two hospitals on the first tier, so exactly one hospital-types
    call should fire total, not one per type."""
    calls = []

    async def fake_search_text(*args, **kwargs):
        return [place("Tower Health Urgent Care", ["health"], lat=40.0, lng=-75.01)]

    async def fake_search_nearby_types(*args, **kwargs):
        included_types = _included_types(args, kwargs)
        calls.append(included_types)
        if "hospital" in included_types:
            # two real hospitals - enough to compare by drive, so no wider ring is searched
            return [place("Reading Hospital", ["hospital"], lat=40.0, lng=-75.02), place("Pottstown Hospital", ["hospital"], lat=40.0, lng=-75.05)]
        return [place("Tower Health Medical Clinic", ["medical_clinic"], lat=40.0, lng=-75.003)]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0)

    hospital_calls = [c for c in calls if "hospital" in c]
    assert len(hospital_calls) == 1, f"expected exactly one combined hospital-types call (found + resolved on tier 1), got {hospital_calls}"
    assert set(hospital_calls[0]) == {"general_hospital", "hospital"}


@pytest.mark.asyncio
async def test_fetch_medical_care_leaves_unavailable_when_no_confident_match(monkeypatch):
    async def fake_search_text(*args, **kwargs):
        return [place("Premier Physical Therapy", ["physiotherapist"])]

    async def fake_search_nearby_types(*args, **kwargs):
        included_types = _included_types(args, kwargs)
        if "hospital" in included_types:
            return [place("ChristianaCare Primary Care at Hockessin", ["hospital"])]
        return [place("Quest Diagnostics Lab", ["doctor"])]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    assert await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0) == []


@pytest.mark.asyncio
async def test_hospital_slot_skips_va_long_term_care_and_closed_hospitals(monkeypatch):
    """10/25 grader round: "Coatesville VA Medical Center" and "Kindred Hospital Philadelphia -
    Havertown" filled the hospital slot on real sheets."""
    async def fake_search_text(*args, **kwargs):
        return []

    async def fake_search_nearby_types(*args, **kwargs):
        if "hospital" in _included_types(args, kwargs):
            closed = place("Crozer-Chester Medical Center", ["hospital"], lat=40.0, lng=-75.004)
            closed["businessStatus"] = "CLOSED_PERMANENTLY"
            return [
                place("Coatesville VA Medical Center", ["hospital"], lat=40.0, lng=-75.001),
                place("Kindred Hospital Philadelphia - Havertown", ["hospital"], lat=40.0, lng=-75.002),
                place("Children's Hospital of Philadelphia", ["hospital"], lat=40.0, lng=-75.003),
                closed,
                place("Bryn Mawr Hospital", ["hospital"], lat=40.0, lng=-75.03),
            ]
        return []

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_search_nearby_types", fake_search_nearby_types)

    results = await svc._fetch_medical_care(None, "fake-key", 40.0, -75.0)

    assert [r.name for r in results] == ["Bryn Mawr Hospital"]


@pytest.mark.asyncio
async def test_proofread_replaces_from_checked_backups_and_confirms_renames(monkeypatch):
    """The AI proofread can only take a confirmed-wrong item out: its slot refills from that list's
    backup (already through every rule; a backup the AI also flagged is skipped), and a rename is
    applied only when Google shows a place by the new name at that spot."""
    from app.models.property import NearbyPlace

    def p(name, tag=None, lat=40.0, lng=-75.0, miles=1.0):
        return NearbyPlace(name=name, place_type=tag, latitude=lat, longitude=lng, distance_miles=miles)

    lists = {
        "medical_care": [p("Coatesville VA Medical Center", "HOSPITAL"), p("AFC Urgent Care", "URGENT CARE")],
        "parks": [p("Chinese chestnut 2"), p("Old Mill Park", lat=40.01)],
        "airports": [p("Lancaster Airport", "MAJOR AIRPORT")],
    }
    backups = {
        "medical_care:hospital": [p("Kindred Hospital", "HOSPITAL", miles=2.0), p("Chester County Hospital", "HOSPITAL", miles=9.0)],
        "parks": [p("Rose Tree Park", miles=1.5)],
    }
    issues = [
        {"id": "medical_care#0", "problem": "wrong_category"},
        {"id": "backup:medical_care:hospital#0", "problem": "wrong_category"},
        {"id": "parks#0", "problem": "not_a_public_place"},
        {"id": "parks#1", "problem": "renamed", "current_name": "Mill Creek Park"},
        {"id": "airports#0", "problem": "wrong_category"},
    ]

    async def fake_search_text(client, api_key, lat, lng, query, radius, **kwargs):
        assert query == "Mill Creek Park"
        return [{"displayName": {"text": "Mill Creek Park"}, "primaryType": "park", "types": ["park"],
                 "location": {"latitude": 40.0105, "longitude": -75.0}, "businessStatus": "OPERATIONAL"}]

    async def no_driving(*args, **kwargs):
        return None

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    monkeypatch.setattr(svc, "_apply_driving_distances", no_driving)

    actions = await svc._apply_proofread(None, "fake-key", 40.0, -75.0, lists, backups, issues)

    assert [x.name for x in lists["medical_care"]] == ["Chester County Hospital", "AFC Urgent Care"]
    assert [x.name for x in lists["parks"]] == ["Mill Creek Park", "Rose Tree Park"]
    assert lists["airports"] == []
    assert any("renamed Old Mill Park -> Mill Creek Park" in a for a in actions)


def test_proofread_ignores_issues_without_a_source():
    from app.services.ai.cheat_sheet_proofread_service import _clean
    cleaned = _clean([
        {"id": "parks#0", "problem": "closed", "explanation": "closed in 2024", "source_url": "https://example.org/news"},
        {"id": "parks#1", "problem": "closed", "explanation": "I think it closed", "source_url": ""},
        {"id": "parks#2", "problem": "too_far", "explanation": "far", "source_url": "https://example.org"},
    ])
    assert [c["id"] for c in cleaned] == ["parks#0"]


def test_shortest_drive_wins_the_hospital_slot_and_far_schools_drop():
    """Bryans Road MD (10/25 random test): across the Potomac the straight-line nearest hospital and
    private schools were 24-27 miles by car."""
    from app.models.property import NearbyPlace
    medical = [NearbyPlace(name="Inova Mount Vernon Hospital", place_type="HOSPITAL", distance_miles=24.4),
               NearbyPlace(name="Urgent Care", place_type="URGENT CARE", distance_miles=12.2)]
    hospital_backups = [NearbyPlace(name="UM Charles Regional Medical Center", place_type="HOSPITAL", distance_miles=17.0)]
    schools = [NearbyPlace(name="Aquinas Montessori", distance_miles=25.5), NearbyPlace(name="Engleside Christian", distance_miles=27.0)]
    school_backups = [NearbyPlace(name="Grace Lutheran", distance_miles=9.0)]

    svc._prefer_shortest_drive(medical, hospital_backups, schools, school_backups)

    assert [m.name for m in medical] == ["UM Charles Regional Medical Center", "Urgent Care"]
    assert [b.name for b in hospital_backups] == ["Inova Mount Vernon Hospital"]
    assert [s.name for s in schools] == ["Grace Lutheran"]
