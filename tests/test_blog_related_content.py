"""Related content at the end of an AI article (owner, 11/15: "more of a footer thing").

The agent's own video, picked sales and attached listings are closing cards placed by code - the AI is not told
about them and writes nothing around them. What went wrong while they were worked into the article:
  - the AI can't watch a video: it saw the title, wrote "My video ... is just below", and the video landed twelve
    paragraphs later (Bryn Mawr seller article, 11/14)
  - working sales into the argument is where the checker kept catching slips: a 16-month-old sale compared with
    "today's median", "one of those 11", "the sales show competing offers" (Gladwyne, 11/14)
The one exception: an article that is ABOUT the agent's own sales or listings still gets their facts.
No network and no database."""
from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.ai import ai_blog_writer_service as w

SALES = [{"id": "804812985848", "address": "26 Old Gulph Rd", "city": "Gladwyne", "state": "PA", "price": 7150000, "beds": 6, "baths": 8,
          "sqft": 12000, "sold_date": "2026-10-08", "image_url": "/media/a.jpg"},
         {"id": "803811190346", "address": "1041 Waverly Rd", "city": "Gladwyne", "state": "PA", "price": 1665000, "sold_date": "2025-07-31", "image_url": None}]
LISTING = {"listing_key": "PAMC123", "address": "12 Elm St", "city": "Ardmore", "state": "PA", "price": 520000, "beds": 3, "baths": 2,
           "sqft": 1500, "status": "Active", "image_url": "/media/b.jpg"}
VIDEO = {"url": "https://www.youtube.com/watch?v=WpLvIF09W1A", "title": "What to Know Before Selling Your House on the Main Line"}


def _article():
    return [{"id": "p0", "type": "paragraph", "data": {"html": "Intro."}},
            {"id": "h1", "type": "heading", "data": {"html": "Prices"}}, {"id": "p1", "type": "paragraph", "data": {"html": "Body."}},
            {"id": "h2", "type": "heading", "data": {"html": "Talk to me"}}, {"id": "p2", "type": "paragraph", "data": {"html": "Closing words."}},
            {"id": "s", "type": "sources", "data": {"items": []}},
            {"id": "c", "type": "cta", "data": {"label": "Contact Me"}}, {"id": "a", "type": "agent_profile", "data": {}}]


# ----------------------------------------------------------------------------- when the AI is told

def test_only_an_article_about_the_agents_own_sales_gets_them_in_the_text():
    about = lambda topic, **more: w.about_own({"topic": topic, **more}, "sales")          # noqa: E731
    assert about("What my recent Gladwyne sales say about pricing right now")
    assert about("Three homes I sold in Wayne this fall") and about("Just sold in Ardmore: what the price tells you")
    assert about("Pricing a home in Media", key_points="- What my last two sales closed for")
    # an ordinary article with sales ticked: they are an extra, not the topic
    assert not about("First-Time Homebuyer Guide to Ardmore: Condo, Townhome or Twin?")
    assert not about("Selling Your Bryn Mawr Home This Spring", key_points="- How fast homes are selling\n- What sold above asking")
    assert not about("How home sales are trending in Media", goal="Understand the market")
    # the agent's personal take is their own words either way - it never turns the extras into the topic
    assert not w.about_own({"topic": "Buying in Ardmore", "personal_take": "My take: my sales here show walkability wins."}, "sales")
    assert w.about_own({"topic": "Just listed in Ardmore: 12 Elm St"}, "listings") and w.about_own({"topic": "Tour my new listing"}, "listings")
    assert not w.about_own({"topic": "How many listings does Ardmore have right now?"}, "listings")


def test_the_ai_is_no_longer_told_about_the_video_or_the_extras():
    brief = {"topic": "Selling your Bryn Mawr home", "video": VIDEO, "include_recent_sales": True, "sale_ids": ["804812985848"]}
    msg, source = w._brief_text(brief, {"name": "Peter", "title": "", "brokerage": "", "market": "", "about": ""}, [], [], None)
    assert "video" not in msg.lower() and "What to Know Before Selling" not in msg and "Old Gulph" not in msg
    # ...and not told to avoid them either: round G (11/15) had "never mention ... a video" in the rules, and an article
    # with no video came back with "the agent's video on the topic is not included here"
    assert "video" not in pipe._ENGINE_RULES.lower() and "video" not in pipe._coverage(brief, 2).lower()


def test_a_picked_sale_is_only_demanded_in_the_text_when_the_article_is_about_the_sales():
    extra = {"topic": "Selling your Gladwyne home this spring", "include_recent_sales": True, "sale_ids": ["804812985848"]}
    topic = {"topic": "What my recent Gladwyne sales say about pricing", "include_recent_sales": True, "sale_ids": ["804812985848"]}
    for brief, wanted in ((extra, 0), (topic, 1)):
        text_sales = SALES if w.about_own(brief, "sales") else []
        chosen = pipe.picked_sales(brief, text_sales)
        missing = [p for p in pipe.code_problems("Homes sold fast this fall.", brief=brief, protections=[], must_mention=chosen) if p["kind"] == "missing_sale"]
        assert len(missing) == wanted
        assert len(pipe._sale_items(text_sales)) == (2 if wanted else 0)


# ----------------------------------------------------------------------------- the cards

def test_the_extras_become_closing_cards_from_the_agents_own_records():
    brief = {"topic": "Gladwyne market", "areas": ["Gladwyne, PA"], "include_recent_sales": True, "sale_ids": ["804812985848", "803811190346"],
             "video": VIDEO}
    sales_card, listings_card, video_card = w.related_blocks(brief, SALES, [LISTING])
    assert sales_card["type"] == "property_showcase" and sales_card["data"]["kind"] == "sales" and sales_card["data"]["title"] == "Recent Sales in Gladwyne"
    first = sales_card["data"]["properties"][0]
    assert (first["address"], first["price"], first["sold_date"], first["status"], first["image_url"]) == ("26 Old Gulph Rd", 7150000, "2026-10-08", "Sold", "/media/a.jpg")
    assert len(sales_card["data"]["properties"]) == 2                                       # every sale the agent ticked, old or new
    assert listings_card["data"]["kind"] == "listings" and listings_card["data"]["title"] == "Homes for Sale"   # Ardmore isn't the article's town
    assert listings_card["data"]["properties"][0]["id"] == "PAMC123" and listings_card["data"]["properties"][0]["status"] == "Active"
    assert video_card["type"] == "video_showcase" and video_card["data"]["title"] == "Related video"
    assert video_card["data"]["videos"] == [{"id": VIDEO["url"], "url": VIDEO["url"], "title": VIDEO["title"], "image": None}]
    assert len({b["id"] for b in (sales_card, listings_card, video_card)}) == 3
    # a hub address that carries its town and ZIP shows the street only (the card has the town on its own line)
    (card,) = w.related_blocks({"topic": "x", "areas": ["Media"], "include_recent_sales": True, "sale_ids": ["1"]},
                               [{"id": "1", "address": "112 Wilton Woods Ln, Media, Pa 19063", "city": "Media", "price": 1310000, "sold_date": "2025-03-22"}], [])
    assert card["data"]["properties"][0]["address"] == "112 Wilton Woods Ln" and card["data"]["properties"][0]["city"] == "Media"
    assert w._street("26 Old Gulph Rd", "Gladwyne") == "26 Old Gulph Rd" and w._street("9 Media Line Rd", "Media") == "9 Media Line Rd"
    assert w._street("12 Elm St", None) == "12 Elm St" and w._street(None, "Media") == ""
    assert w.related_blocks({"topic": "x"}, [], []) == []
    assert w.related_blocks({"topic": "x", "video": {"url": "  "}}, [], []) == []


def test_the_automatic_recent_sales_set_shows_only_the_recent_ones():
    from datetime import datetime, timedelta, timezone
    day = lambda n: (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()   # noqa: E731
    sales = [{"id": str(i), "address": f"{i} Main St", "city": "Media", "price": 500000 + i, "sold_date": day(30 * i)} for i in range(1, 9)]
    sales.append({"id": "old", "address": "9 Old Rd", "city": "Media", "price": 400000, "sold_date": day(500)})
    (card,) = w.related_blocks({"topic": "Media market", "areas": ["Media"], "include_recent_sales": True}, sales, [])
    assert [p["id"] for p in card["data"]["properties"]] == ["1", "2", "3", "4"] and card["data"]["title"] == "Recent Sales in Media"
    (card,) = w.related_blocks({"topic": "Media market", "include_recent_sales": True}, [sales[-1]], [])
    assert [p["id"] for p in card["data"]["properties"]] == ["old"] and card["data"]["title"] == "Recent Sales in the Area"


def test_related_cards_sit_after_the_article_and_before_the_closing_button():
    blocks = _article()
    related = w.related_blocks({"topic": "x", "areas": ["Gladwyne"], "include_recent_sales": True, "sale_ids": ["804812985848"], "video": VIDEO}, SALES[:1], [])
    out = w.place_related(blocks, related)
    types = [b["type"] for b in out]
    assert types == ["paragraph", "heading", "paragraph", "heading", "paragraph", "sources", "property_showcase", "video_showcase", "cta", "agent_profile"]
    assert "youtube" not in types and "property_card" not in types
    # nothing is added to the article's own text: what the fact-check and the grader read is unchanged
    assert w.blocks_to_text(out).replace("Contact Me", "").strip() == w.blocks_to_text(blocks).replace("Contact Me", "").strip()
    assert w.place_related(blocks, []) == blocks
    no_button = [b for b in blocks if b["type"] != "cta"]
    assert [b["type"] for b in w.place_related(no_button, related)][-3:] == ["property_showcase", "video_showcase", "agent_profile"]
    bare = blocks[:5]
    assert [b["type"] for b in w.place_related(bare, related)][-2:] == ["property_showcase", "video_showcase"]
