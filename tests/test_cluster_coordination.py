"""app/services/cluster.py - the shared locks, leases and speed limits every API copy checks
(WEB_WORKERS > 1), against the real local database. Each test races many callers at once, the way
3 copies would."""
import asyncio
import time
import uuid

from app.services import cluster


def _k(prefix: str) -> str:
    return f"test-{prefix}-{uuid.uuid4().hex[:10]}"


def run(coro):
    async def wrapped():
        from app.database import engine
        try:
            return await coro
        finally:
            await engine.dispose()   # close this loop's connections now (each test runs its own loop)
    return asyncio.run(wrapped())


def test_only_one_of_many_racers_gets_a_lease():
    async def go():
        key = _k("one")
        tokens = await asyncio.gather(*(cluster.acquire("test", key, 30) for _ in range(12)))
        got = [t for t in tokens if t]
        assert len(got) == 1
        assert await cluster.alive("test", key)
        await cluster.release("test", key, got[0])
        assert not await cluster.alive("test", key)
        assert await cluster.acquire("test", key, 30)   # free again
        await cluster.release("test", key)
    run(go())


def test_expired_lease_can_be_taken_and_old_holder_cannot_renew():
    async def go():
        key = _k("exp")
        first = await cluster.acquire("test", key, 0.3)
        assert first
        assert await cluster.acquire("test", key, 30) is None
        await asyncio.sleep(0.5)           # the first holder "died" - stopped renewing
        second = await cluster.acquire("test", key, 30)
        assert second and second != first
        assert not await cluster.renew("test", key, first, 30)   # the dead holder can't steal it back
        await cluster.release("test", key, first)                # nor delete it
        assert await cluster.alive("test", key)
        await cluster.release("test", key, second)
    run(go())


def test_per_owner_limit_holds_under_a_race():
    async def go():
        owner = _k("owner")
        allow = lambda n, _amt: n < 2   # "at most 2 at once"
        tokens = await asyncio.gather(*(cluster.acquire("test_cc", f"{owner}:{i}", 30, owner_id=owner, allow=allow) for i in range(10)))
        got = [(i, t) for i, t in enumerate(tokens) if t]
        assert len(got) == 2
        await cluster.release("test_cc", f"{owner}:{got[0][0]}", got[0][1])
        assert await cluster.acquire("test_cc", f"{owner}:x", 30, owner_id=owner, allow=allow)
        for i, t in got[1:]:
            await cluster.release("test_cc", f"{owner}:{i}", t)
        await cluster.release("test_cc", f"{owner}:x")
    run(go())


def test_amount_limit_holds_under_a_race():
    async def go():
        owner = _k("amt")
        allow = lambda _n, amt: amt + 0.75 <= 2.0   # room for two $0.75 holds in $2.00
        tokens = await asyncio.gather(*(cluster.acquire("test_amt", f"r{i}", 30, owner_id=owner, amount=0.75, allow=allow) for i in range(8)))
        assert sum(1 for t in tokens if t) == 2
        for i, t in enumerate(tokens):
            if t:
                await cluster.release("test_amt", f"r{i}", t)
    run(go())


def test_lease_block_renews_and_gives_back():
    async def go():
        key = _k("ctx")
        async with cluster.lease("test", key, ttl=1.2) as tok:
            assert tok
            await asyncio.sleep(2.0)            # longer than ttl: kept alive by the beat
            assert await cluster.alive("test", key)
            async with cluster.lease("test", key, ttl=5) as other:
                assert other is None            # someone else (another copy) can't take it
        assert not await cluster.alive("test", key)
    run(go())


def test_lease_given_back_after_an_error():
    async def go():
        key = _k("err")
        try:
            async with cluster.lease("test", key, ttl=30) as tok:
                assert tok
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not await cluster.alive("test", key)
    run(go())


def test_db_lock_one_at_a_time_and_try_lock():
    async def go():
        name = _k("lock")
        spans = []

        async def holder():
            async with cluster.db_lock(name):
                s = time.perf_counter()
                await asyncio.sleep(0.15)
                spans.append((s, time.perf_counter()))

        await asyncio.gather(*(holder() for _ in range(4)))
        spans.sort()
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            assert s2 >= e1 - 0.01
        async with cluster.try_db_lock(name) as a:
            assert a
            async with cluster.try_db_lock(name) as b:
                assert b is False
        async with cluster.try_db_lock(name) as c:
            assert c
    run(go())


def test_db_lock_timeout_and_cancel_do_not_leak():
    async def go():
        name = _k("leak")
        async with cluster.db_lock(name):
            try:
                async with cluster.db_lock(name, timeout=0.3):
                    raise AssertionError("should not get it")
            except TimeoutError:
                pass
            waiter = asyncio.create_task(cluster.db_lock(name).__aenter__())
            await asyncio.sleep(0.2)
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass
        # nothing leaked into the pool: the lock is free for a fresh caller
        async with cluster.try_db_lock(name) as ok:
            assert ok
    run(go())


def test_rate_limit_exact_under_a_race():
    async def go():
        key = _k("rate")
        results = await asyncio.gather(*(cluster.rate_hit(key, 8, 60) for _ in range(30)))
        assert sum(results) == 8
    run(go())


def test_rate_limits_all_or_nothing():
    async def go():
        a, b = _k("ra"), _k("rb")
        assert await cluster.rate_hits([(a, 5, 60), (b, 1, 3600)])
        assert not await cluster.rate_hits([(a, 5, 60), (b, 1, 3600)])   # b is full ...
        assert round(await cluster.rate_count(a, 60)) == 1                # ... so a wasn't counted again
    run(go())


def test_cleanup_runs():
    async def go():
        out = await cluster.cleanup()
        assert set(out) == {"leases", "windows"}
    run(go())


def test_chat_slots_shared_and_handed_back_in_the_same_request():
    from app.services.ai import ai_rate_limit_service as rl

    async def go():
        user = _k("chat")

        async def request(hold):
            try:
                await rl._limiter.acquire_concurrency(user, 2)
            except rl.AIRateLimitExceeded:
                return "refused"
            try:
                await asyncio.sleep(hold)
                return "ran"
            finally:
                await rl.release(user)   # the route's finally - same task as the acquire

        first = await asyncio.gather(request(0.6), request(0.6), request(0.6))
        assert sorted(first) == ["ran", "ran", "refused"]
        assert not await cluster.alive_keys("ai_chat", [])   # (no-op call path)
        again = await asyncio.gather(request(0.1), request(0.1))   # slots came back at once, not after 90 s
        assert again == ["ran", "ran"]
    run(go())


def test_chat_burst_shared():
    from app.services.ai import ai_rate_limit_service as rl

    async def go():
        user = _k("burst")
        ok = 0
        for _ in range(12):
            try:
                await rl._limiter.check_burst(user, 8)
                ok += 1
            except rl.AIRateLimitExceeded:
                pass
        assert ok == 8
    run(go())


def test_voice_session_ended_on_another_copy_frees_the_slot():
    import app.main  # noqa: F401 - load the app first (the voice module has an import loop when loaded alone)
    from app.services.ai import ai_realtime_service as rt

    async def go():
        user, s1, s2 = _k("voice"), _k("s1"), _k("s2")
        await rt._session_limiter.acquire(user, s1, 1, 600)
        try:
            await rt._session_limiter.acquire(user, s2, 1, 600)
            raise AssertionError("second voice session should be refused")
        except rt.AIRateLimitExceeded:
            pass
        await rt._session_limiter.release(s1)          # by session id - works from any copy
        await rt._session_limiter.acquire(user, s2, 1, 600)
        await rt._session_limiter.release(s2)
    run(go())


def test_mailbox_hour_cap_is_20():
    from app.services import agent_mailbox_service as mb

    async def go():
        user = _k("mail")
        res = [await mb._take_hour_slot(user, "google") for _ in range(25)]
        assert sum(res) == 20 and mb.HOURLY_CAP == {"google": 20, "microsoft": 20}
    run(go())
