"""11/16 out-of-state test round (Bethesda MD, Haddonfield NJ, Wilmington DE, written on live).

The Bethesda town card read "Transit nearby: Bethesda (trolley)": the map data tags the Metro station
`station=subway` and ALSO `light_rail=yes` (a light-rail line is being added to it), and any light-rail tag
used to win. A subway station is a train station whatever else stops there."""
from app.services.areas import town_map_service as tms


def test_a_subway_station_with_a_light_rail_tag_is_not_a_trolley_stop():
    bethesda = {"railway": "station", "name": "Bethesda", "network": "Washington Metro", "station": "subway", "subway": "yes", "light_rail": "yes", "railway:ref": "A09"}
    assert tms.station_kind(bethesda) == "train"
    # Philadelphia's 15th Street: subway station that trolleys also stop at
    assert tms.station_kind({"railway": "station", "name": "15th Street", "network": "SEPTA", "station": "subway", "subway": "yes", "tram": "yes", "train": "yes"}) == "train"
    assert tms.station_kind({"railway": "station", "name": "Somewhere", "subway": "yes", "light_rail": "yes"}) == "train"


def test_real_trolley_and_light_rail_stops_still_read_as_trolley():
    # the Main Line's SEPTA Metro M stops, exactly as the map data tags them (they also carry train=yes)
    assert tms.station_kind({"railway": "station", "name": "Ardmore Junction", "network": "SEPTA", "station": "light_rail", "light_rail": "yes", "train": "yes"}) == "trolley"
    assert tms.station_kind({"railway": "station", "name": "19th Street", "network": "SEPTA", "station": "tram", "tram": "yes", "subway": "no", "light_rail": "no"}) == "trolley"
    assert tms.station_kind({"railway": "station", "name": "22nd Street", "network": "SEPTA", "station": "light_rail", "tram": "yes", "subway": "no"}) == "trolley"
    # no station type given, only the light-rail flag
    assert tms.station_kind({"railway": "station", "name": "A Stop", "network": "X", "light_rail": "yes"}) == "trolley"
    assert tms.station_kind({"railway": "station", "name": "A Stop", "network": "X", "tram": "yes", "subway": "no"}) == "trolley"


def test_plain_rail_stations_are_unchanged():
    assert tms.station_kind({"railway": "station", "name": "Medical Center", "network": "Washington Metro", "station": "subway", "subway": "yes"}) == "train"
    assert tms.station_kind({"railway": "station", "name": "Ardmore", "network": "Amtrak;SEPTA", "train": "yes"}) == "train"


# --- Wilmington, DE: three small things the article showed ---------------------------------------------------------

def test_a_fee_only_a_few_listings_report_stays_off_the_payment_card():
    from app.services.ai import ai_blog_pipeline_service as pipe
    # 36 of the 240 townhomes for sale listed a fee (median $28): not typical, so the card adds no fee line
    wilmington = {"hoa": {"townhouse": {"homes": 36, "of": 240, "median_monthly": 28}, "condo": {"homes": 53, "of": 55, "median_monthly": 636}}}
    fees = pipe.example_fees(wilmington)
    assert "townhouse" not in fees and fees["condo"]["monthly"] == 636
    # near the entry price most homes are townhomes -> no fee at all (never the condo fee on a townhome example)
    assert pipe.entry_fee({"near_homes": 128, "near_condos": 20, "near_townhomes": 100}, fees) is None
    # Bethesda: every condo listing gives a fee -> the card keeps it
    bethesda = pipe.example_fees({"hoa": {"condo": {"homes": 101, "of": 101, "median_monthly": 895}}})
    assert pipe.entry_fee({"near_homes": 47, "near_condos": 40, "near_townhomes": 5}, bethesda)["monthly"] == 895
    # exactly half counts; an older stored fact with no total is treated as before
    assert "townhouse" in pipe.example_fees({"hoa": {"townhouse": {"homes": 20, "of": 40, "median_monthly": 150}}})
    assert "townhouse" in pipe.example_fees({"hoa": {"townhouse": {"homes": 38, "median_monthly": 215}}})
    assert pipe.example_fees({"hoa": {"townhouse": {"homes": 9, "of": 9, "median_monthly": 215}}}) == {}


def test_a_train_ride_does_not_bring_the_drive_time_note():
    from app.services.ai import ai_blog_pipeline_service as pipe
    drive = "Drive times: Google Maps estimates"
    train = "On weekends, trains run about every two hours and take about 48 minutes to 30th Street."
    assert not any(n.startswith(drive) for n in pipe.how_counted_notes(train))
    assert any(n.startswith(drive) for n in pipe.how_counted_notes("Center City is about 35 minutes by car on a weekday morning."))
    assert any(n.startswith(drive) for n in pipe.how_counted_notes("It is about 25 minutes to King of Prussia at 8 AM (Google Maps estimate)."))
    assert any(n.startswith(drive) for n in pipe.how_counted_notes(train + " The drive is about 40 minutes to Center City."))


def test_an_instruction_heading_is_not_an_ask_me_topic():
    from app.services.ai import ai_blog_writer_service as writer

    def topic_under(heading):
        blocks = [{"id": "h", "type": "heading", "data": {"html": heading}},
                  {"id": "p", "type": "paragraph", "data": {"html": "Two homes at the same price can come with very different monthly bills."}},
                  {"id": "a", "type": "ask_agent", "data": {}}]
        return (writer._name_ask_topics(blocks)[2].get("data") or {}).get("topic")

    assert topic_under("Compare total monthly costs, not just the sale price") is None      # was "Ask me about compare total monthly costs..."
    assert topic_under("Get pre-approved before you tour") is None
    assert topic_under("Don't skip the inspection") is None
    assert topic_under("Delaware vs. Pennsylvania taxes for movers") == "Delaware vs. Pennsylvania taxes for movers"
    assert topic_under("Loan options and first-time buyer help") in ("loan options and first-time buyer help", "Loan options and first-time buyer help")
    assert topic_under("Pricing your home") in ("pricing your home", "Pricing your home")


def test_real_topics_that_start_like_a_verb_are_kept():
    from app.services.ai import ai_blog_writer_service as writer
    for topic in ("use and occupancy certificates", "drive times to Center City", "price reductions", "the Realty Transfer Fee",
                  "down payment help", "which township a home is in", "seller disclosures", "walk scores", "plan documents for a condo"):
        assert writer._ask_block(topic)["data"].get("topic") == topic, topic
    for instruction in ("compare total monthly costs, not just the sale price", "Get pre-approved first", "check the tax bill"):
        assert writer._ask_block(instruction)["data"] == {}, instruction
