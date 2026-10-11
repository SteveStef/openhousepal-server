from app.services.areas.canonical_key import compute_canonical_key, normalize_geography_name


def test_villanova_spelling_variants_share_one_canonical_key():
    variants = ["Villanova", "Villanova PA", "Villanova, PA", "Villanova, Pennsylvania", "Villanova Township"]
    keys = {compute_canonical_key(name, "PA", "city") for name in variants}
    assert len(keys) == 1


def test_state_is_case_and_whitespace_insensitive():
    a = compute_canonical_key("Villanova", "pa", "city")
    b = compute_canonical_key("Villanova", " PA ", "city")
    assert a == b


def test_genuinely_different_towns_stay_distinct():
    assert compute_canonical_key("Villanova", "PA", "city") != compute_canonical_key("Wayne", "PA", "city")


def test_different_states_stay_distinct_even_with_same_name():
    assert compute_canonical_key("Villanova", "PA", "city") != compute_canonical_key("Villanova", "OH", "city")


def test_rittenhouse_square_variants_under_same_parent_share_one_key():
    # name is assumed already resolved to a clean geography name by the caller (see
    # site_routes.add_my_served_area, which resolves via geocoding.resolve_named_place before
    # calling compute_canonical_key) - only the parent's spelling/casing varies here.
    variants = [
        ("Rittenhouse Square", "Philadelphia"),
        ("Rittenhouse Square", "Philadelphia, PA"),
        ("rittenhouse square", "philadelphia pa"),
    ]
    keys = {compute_canonical_key(name, "PA", "neighborhood", parent) for name, parent in variants}
    assert len(keys) == 1


def test_same_neighborhood_name_under_different_parents_stays_distinct():
    key_a = compute_canonical_key("Oak Grove", "PA", "neighborhood", "Springfield")
    key_b = compute_canonical_key("Oak Grove", "PA", "neighborhood", "Media")
    assert key_a != key_b


def test_city_and_neighborhood_with_same_name_stay_distinct():
    city_key = compute_canonical_key("Wayne", "PA", "city")
    neighborhood_key = compute_canonical_key("Wayne", "PA", "neighborhood", "Radnor")
    assert city_key != neighborhood_key


def test_municipality_suffix_stripped_only_as_trailing_word():
    assert normalize_geography_name("Radnor Township") == normalize_geography_name("Radnor")
    # "Township" isn't stripped mid-name / doesn't eat a town whose real name contains a similar word
    assert normalize_geography_name("Villanova") != normalize_geography_name("Villa")


def test_does_not_over_normalize_genuinely_different_names():
    assert normalize_geography_name("Bryn Mawr") != normalize_geography_name("Broomall")
    assert normalize_geography_name("Media") != normalize_geography_name("Malvern")
