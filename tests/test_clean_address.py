"""mls_mapper.clean_address - the street line shown on every card (audit 11/13)."""
from app.utils.mls_mapper import clean_address


def test_plain_street_kept():
    assert clean_address("2074 FOX CREEK RD") == "2074 FOX CREEK RD"


def test_unparsed_drops_city_state():
    assert clean_address("2074 FOX CREEK RD,BERWYN,PA,19312,CHESTER") == "2074 FOX CREEK RD"


def test_several_house_numbers_keep_their_street():
    assert clean_address("170, 200, 220 OLD STATE RD") == "170/200/220 OLD STATE RD"
    assert clean_address("170, 200, 220 OLD STATE RD,BERWYN,PA,19312,CHESTER") == "170/200/220 OLD STATE RD"
    assert clean_address("1402, 1410, 1416 QUINCY ST NW,WASHINGTON,DC,20011") == "1402/1410/1416 QUINCY ST NW"


def test_number_without_street_unchanged():
    # Nothing better to show - same as before.
    assert clean_address("0,CHESTER GAP,VA,22623") == "0"


def test_empty():
    assert clean_address("") == ""


def test_display_street_repairs_old_rows():
    from app.utils.mls_mapper import display_street
    assert display_street("170", "170, 200, 220 OLD STATE RD,BERWYN,PA,19312,CHESTER") == "170/200/220 OLD STATE RD"
    assert display_street("129", "VA,WARREN") == "129"          # nothing better - unchanged
    assert display_street("2074 FOX CREEK RD", "anything") == "2074 FOX CREEK RD"
    assert display_street(None, None) is None


def test_pdfs_are_not_photos():
    from app.utils.mls_mapper import card_photo, is_photo_url, real_photos
    pdf = "https://bright-media02.prd.brightmls.com/bright/documents/0000/8038/9689/4650/803896894650_ON3bS9iDxJpEPOgR.pdf"
    doc = "https://bright-media02.prd.brightmls.com/bright/documents/0000/8048/7384/4622/804873844622_z_42p1"
    jpg = "http://bright-media02.prd.brightmls.com/bright/images/0000/8053/8168/5112/805381685112_2048_1536_WM_G2.jpg"
    assert not is_photo_url(pdf) and not is_photo_url(doc) and is_photo_url(jpg)
    assert real_photos([pdf, jpg, None, "", doc]) == [jpg]
    assert card_photo(pdf, [doc, jpg]) == jpg
    assert card_photo(jpg, [pdf]) == jpg
    assert card_photo(None, [pdf]) is None


def test_mapper_skips_pdf_photos():
    from app.utils.mls_mapper import map_reso_to_internal
    pdf = "https://x.brightmls.com/bright/documents/1/a.pdf"
    jpg = "https://x.brightmls.com/bright/images/1/a.jpg"
    out = map_reso_to_internal({"ListingKey": "1", "ListPictureURL": pdf, "UnparsedAddress": "1 MAIN ST"}, photo_map={"1": [pdf, jpg]})
    assert out["photos"] == [jpg] and out["img_src"] == jpg
