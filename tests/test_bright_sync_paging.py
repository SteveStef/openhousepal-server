"""The Bright listings / sold sync paging (11/02): paged by time instead of by position, the
bookmark saved after every page, and a time limit per cycle. Runs the real run_global_sync loop
against a fake Bright feed - never the real Bright API, never the database (the per-listing save,
the bookmark store and the deletions step are stand-ins)."""
import itertools

import pytest

from app.services import property_sync_service as pss


class FakeBright:
    """Bright's "changed since" feed: listings ordered by ModificationTimestamp (oldest first),
    filtered gt/ge a time, $top/$skip. `on_fetch` can change listings between requests, the way
    agents keep editing while a long catch-up runs. `fail_on` makes that request fail (a 401)."""

    def __init__(self, rows, on_fetch=None, fail_on=None):
        self.rows = {r["ListingKey"]: dict(r) for r in rows}
        self.on_fetch = on_fetch
        self.fail_on = fail_on
        self.calls = 0

    def touch(self, key, ts):
        self.rows[key]["ModificationTimestamp"] = ts

    async def fetch(self, since, top=200, skip=0, op="gt"):
        self.calls += 1
        if self.on_fetch:
            self.on_fetch(self, self.calls)
        if self.fail_on and self.calls == self.fail_on:
            raise RuntimeError("MLS API Error: 401")
        keep = [r for r in self.rows.values() if (r["ModificationTimestamp"] >= since if op == "ge" else r["ModificationTimestamp"] > since)]
        keep.sort(key=lambda r: r["ModificationTimestamp"])  # Bright breaks ties in no promised order
        return [dict(r) for r in keep[skip:skip + top]]


def ts(n: int) -> str:
    return f"2026-11-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"  # Bright: to the second


def make_rows(n, same_time_every=1):
    # `same_time_every` > 1 puts that many listings on one timestamp (Bright's bulk updates do)
    return [{"ListingKey": f"L{i:05d}", "ModificationTimestamp": ts(1000 + i // same_time_every)} for i in range(n)]


class Harness:
    """A PropertySyncService whose database bits are stand-ins: records which listing versions
    were handled and keeps the bookmark in memory."""

    def __init__(self, bright, bookmark=ts(0), fail_keys=()):
        self.svc = pss.PropertySyncService.__new__(pss.PropertySyncService)
        self.svc.SYNC_KEY = "k"
        self.bookmark = bookmark
        self.handled = []  # (key, timestamp) per listing handled
        self.saves = []
        self.fail_keys = set(fail_keys)
        svc = self.svc

        async def get_last(db, key=None, fallback_hours=24):
            return self.bookmark

        async def update_last(db, t, key=None):
            self.bookmark = t
            self.saves.append(t)

        self.held = {}  # listing -> the Bright version saved (what the properties table holds)

        async def sync_one(db, raw, photo_map, allow_inactive_insert=False):
            if raw["ListingKey"] in self.fail_keys:
                self.fail_keys.discard(raw["ListingKey"])  # fails once, then works on the retry
                raise RuntimeError("boom")
            self.handled.append((raw["ListingKey"], raw["ModificationTimestamp"]))
            self.held[raw["ListingKey"]] = raw["ModificationTimestamp"]
            return None

        async def not_yet_held(db, rows, bookmark, need_close_price=False):
            # same rule as the real one, against `held` instead of the database
            return [r for r in rows if not (r["ModificationTimestamp"] <= bookmark and self.held.get(r["ListingKey"]) == r["ModificationTimestamp"])]

        svc._not_yet_held = not_yet_held

        async def nothing(*a, **k):
            return 0

        svc.get_last_sync_time = get_last
        svc.update_last_sync_time = update_last
        svc._sync_single_property = sync_one
        svc._sync_deletions = nothing
        svc._propagate_property_change = nothing

    def latest_missing(self, bright):
        """Listings whose newest version was never handled."""
        got = set(self.handled)
        return [k for k, r in bright.rows.items() if (k, r["ModificationTimestamp"]) not in got]


class _NoDb:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def bright(monkeypatch):
    holder = {}

    def use(fake):
        monkeypatch.setattr(pss.bright_mls_service, "get_properties_modified_since", fake.fetch)
        monkeypatch.setattr(pss.bright_mls_service, "get_closed_properties_modified_since", fake.fetch)
        holder["fake"] = fake
        return fake

    async def no_media(keys):
        return {}

    monkeypatch.setattr(pss.bright_mls_service, "get_media_for_properties", no_media)
    monkeypatch.setattr(pss, "AsyncSessionLocal", lambda: _NoDb())
    monkeypatch.setattr(pss, "SYNC_PAGE_SIZE", 50)
    return use


async def old_skip_paging(fake, since, page=50):
    """The way it worked before 11/02 - by position - to show what it missed."""
    seen, skip = [], 0
    while True:
        rows = await fake.fetch(since, top=page, skip=skip)
        if not rows:
            break
        seen += [(r["ListingKey"], r["ModificationTimestamp"]) for r in rows]
        if len(rows) < page:
            break
        skip += page
    return seen


def edits_during_run(fake, call):
    # while the catch-up runs, every few requests agents edit 3 listings it already passed
    if call % 2 == 0:
        handled_keys = sorted(fake.rows)[: call * 20]
        for j, k in enumerate(handled_keys[-3:]):
            fake.touch(k, ts(50000 + call * 10 + j))


@pytest.mark.asyncio
async def test_every_listing_handled_including_edits_during_the_run(bright):
    fake = bright(FakeBright(make_rows(1000), on_fetch=edits_during_run))
    h = Harness(fake)
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []


@pytest.mark.asyncio
async def test_old_position_paging_really_missed_listings():
    fake = FakeBright(make_rows(1000), on_fetch=edits_during_run)
    seen = set(await old_skip_paging(fake, ts(0)))
    missed = [k for k, r in fake.rows.items() if (k, r["ModificationTimestamp"]) not in seen]
    assert missed  # the problem the new paging fixes


@pytest.mark.asyncio
async def test_many_listings_on_one_timestamp_across_pages(bright):
    # bulk updates: 120 listings share each timestamp, more than a page (50)
    fake = bright(FakeBright(make_rows(1000, same_time_every=120)))
    h = Harness(fake)
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []
    assert len(h.handled) == len(set(h.handled))  # each handled once


@pytest.mark.asyncio
async def test_bookmark_saved_every_page_and_a_restart_picks_up_where_it_left(bright):
    fake = bright(FakeBright(make_rows(1000)))
    h = Harness(fake)
    await h.svc.run_global_sync(max_pages=7)  # the server restarts after 7 pages
    assert len(h.saves) >= 6
    first = len(h.handled)
    await h.svc.run_global_sync()  # the next cycle
    assert h.latest_missing(fake) == []
    assert len(h.handled) - 1000 <= 50  # at most about one page done twice, not the whole run


@pytest.mark.asyncio
async def test_bright_error_mid_run_loses_nothing(bright):
    fake = bright(FakeBright(make_rows(1000), fail_on=9))  # the 401 on the 9th request
    h = Harness(fake)
    await h.svc.run_global_sync()
    assert 0 < len(h.handled) < 1000 and h.saves
    fake.fail_on = None
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []


@pytest.mark.asyncio
async def test_a_listing_that_fails_is_retried_next_cycle(bright):
    fake = bright(FakeBright(make_rows(300)))
    h = Harness(fake, fail_keys={"L00120"})
    await h.svc.run_global_sync()
    assert ("L00120", fake.rows["L00120"]["ModificationTimestamp"]) not in h.handled
    assert h.bookmark < fake.rows["L00120"]["ModificationTimestamp"]  # bookmark stopped before it
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []


@pytest.mark.asyncio
async def test_time_limit_stops_the_cycle_and_the_next_one_continues(bright):
    fake = bright(FakeBright(make_rows(1000)))
    h = Harness(fake)
    clock = itertools.count(0, 10)  # each check of the clock is 10 s later
    pss.time.monotonic, real = (lambda: next(clock)), pss.time.monotonic
    try:
        await h.svc.run_global_sync(max_seconds=25)
    finally:
        pss.time.monotonic = real
    assert 0 < len(h.handled) < 1000 and h.saves
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []


@pytest.mark.asyncio
async def test_sold_sync_uses_the_same_paging(bright):
    fake = bright(FakeBright(make_rows(400, same_time_every=70), on_fetch=edits_during_run))
    h = Harness(fake)
    h.svc.CLOSED_SYNC_KEY = "c"
    await h.svc.run_closed_sync(max_pages=3)
    await h.svc.run_closed_sync()
    assert h.latest_missing(fake) == []


@pytest.mark.asyncio
async def test_nothing_new_keeps_the_bookmark(bright):
    fake = bright(FakeBright(make_rows(10)))
    h = Harness(fake, bookmark=ts(99999))
    await h.svc.run_global_sync()
    assert h.handled == [] and h.saves == []


@pytest.mark.asyncio
async def test_look_back_catches_a_late_arrival_and_costs_nothing_extra(bright):
    fake = bright(FakeBright(make_rows(300)))
    h = Harness(fake)
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []
    done = len(h.handled)
    # Bright makes one change visible late, stamped 5 minutes BEFORE our bookmark
    late = pss.datetime.strptime(h.bookmark, "%Y-%m-%dT%H:%M:%SZ") - pss.timedelta(minutes=5)
    fake.rows["L99999"] = {"ListingKey": "L99999", "ModificationTimestamp": late.strftime("%Y-%m-%dT%H:%M:%SZ")}
    await h.svc.run_global_sync()
    assert h.latest_missing(fake) == []  # picked up
    assert len(h.handled) - done == 1  # and nothing already held was redone


@pytest.mark.asyncio
async def test_look_back_never_moves_the_bookmark_backwards(bright):
    fake = bright(FakeBright(make_rows(200)))
    h = Harness(fake)
    await h.svc.run_global_sync()
    before = h.bookmark
    await h.svc.run_global_sync()  # nothing new: the look-back rereads the last minutes only
    assert h.bookmark == before
    assert all(s >= before for s in h.saves[h.saves.index(before):])


def test_look_back_time():
    assert pss._look_back("2026-11-01T05:06:03Z") == "2026-11-01T04:56:03Z"
    assert pss._look_back("odd-format") == "odd-format"
