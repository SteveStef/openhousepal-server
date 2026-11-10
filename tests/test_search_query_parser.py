"""The website search box (hero + Search tab): plain sentences -> filters, no AI. 11/09 deep pass."""
import pytest
from app.services.website.search_query_parser import parse_search_query as p

own = ["Ardmore", "Bala Cynwyd", "Bryn Mawr", "Gladwyne", "Haverford", "Radnor", "Villanova", "Wayne"]
other = ["Malvern, PA", "Philadelphia, PA", "Berwyn, PA", "Media, PA", "King Of Prussia, PA", "Newtown Square, PA", "West Chester, PA", "Wayne, NJ", "Exton, PA", "Paoli, PA"]
cases = [
 ("5 bed home in Gladwyne under $3.5M with 1 acre", dict(min_beds=5, max_price=3.5e6, min_lot_size_acres=1.0, city="Gladwyne")),
 ("under $1,500,000 in Ardmore", dict(max_price=1.5e6, city="Ardmore")),
 ("$500,000 - $750,000 Wayne", dict(min_price=5e5, max_price=7.5e5, city="Wayne")),
 ("between 1 and 2 million in Haverford", dict(min_price=1e6, max_price=2e6, city="Haverford")),
 ("4+ beds in Radnor", dict(min_beds=4, city="Radnor")),
 ("four bedroom in Wayne", dict(min_beds=4, city="Wayne")),
 ("3 bd", dict(min_beds=3)),
 ("3-4 beds 2.5 baths", dict(min_beds=3, min_baths=2.5)),
 ("townhome in Wayne", dict(property_type_flag="is_town_house", city="Wayne")),
 ("half acre lot in Wayne", dict(min_lot_size_acres=0.5, city="Wayne")),
 ("2 acres", dict(min_lot_size_acres=2.0)),
 ("19087", dict(zip="19087")),
 ("Malvern", dict(city="Malvern, PA")),
 ("homes in Malvern under 1m", dict(city="Malvern, PA", max_price=1e6)),
 ("Wayne or Berwyn under 900k", dict(cities=["Wayne", "Berwyn, PA"], max_price=9e5)),
 ("radnor schools 4 bed", dict(school_district="radnor", min_beds=4)),
 ("homes in Wayne with radnor schools", dict(city="Wayne", school_district="radnor")),
 ("lower merion school district", dict(school_district="merion", school_words=["lower", "merion"])),
 ("tredyffrin easttown sd", dict(school_district="easttown")),
 ("house with a media room in Wayne", dict(city="Wayne")),
 ("Media PA 3 beds", dict(city="Media, PA", min_beds=3)),
 # home features (owner, 11/09) - searched in the listing's checkboxes + description, no AI
 ("pool", dict(features=["pool"], unmatched=None)),
 ("first floor primary bedroom", dict(features=["first_floor_primary"], unmatched=None)),
 ("4 bed with a pool in Malvern", dict(features=["pool"], min_beds=4, cities=["Malvern, PA"], unmatched=None)),
 ("community pool", dict(features=["community_pool"])),
 ("main level master suite under 900k", dict(features=["first_floor_primary"], max_price=900000)),
 ("primary bedroom on the first floor", dict(features=["first_floor_primary"], unmatched=None)),
 ("finished basement and 2 car garage no hoa", dict(features=["finished_basement", "garage", "no_hoa"], unmatched=None)),
 ("cul de sac fenced yard", dict(features=["cul_de_sac", "fenced_yard"], unmatched=None)),
 ("colonial with sunroom", dict(unmatched=None, features=["sunroom", "colonial"])),
 ("Cape May", dict(features=None)),
 ("wine cellar", dict(features=["wine_cellar"])),
 ("home with a barn", dict(features=["barn"], unmatched=None)),
 ("Media room", dict(unmatched=["media", "room"], cities=None)),
 ("1.2 million", dict(min_price=1020000, max_price=1380000)),
 ("around $900,000", dict(min_price=765000, max_price=1035000)),
 ("under 900", dict()),
 ("Bryn mawr 4 beds over 1.5m under 3m", dict(city="Bryn Mawr", min_beds=4, min_price=1.5e6, max_price=3e6)),
 ("king of prussia condo", dict(city="King Of Prussia, PA", property_type_flag="is_condo")),
 ("5br 4ba Wayne", dict(min_beds=5, min_baths=4.0, city="Wayne")),
 ("2 bed condo under 400k", dict(min_beds=2, property_type_flag="is_condo", max_price=4e5)),
 ("$2m+ in villanova", dict(city="Villanova")),
 ("", dict()),
]


@pytest.mark.parametrize("q,want", cases)
def test_search_sentence(q, want):
    got = p(q, own, other)
    for k, v in want.items():
        assert got.get(k) == v, (q, k, got)
    if not want:
        assert not got, got
