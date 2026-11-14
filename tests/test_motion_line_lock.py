"""The AWS video line is checked and started by one API copy at a time (studio_motion_service._line_db_lock),
even with several copies (WEB_WORKERS > 1) - against the real local database."""
import asyncio
import time

from app.services.studio_motion_service import _line_db_lock


def test_two_copies_never_inside_the_line_step_together():
    async def go():
        spans = []

        async def copy(i):
            async with _line_db_lock():   # the database lock alone (each copy has its own in-memory lock)
                start = time.perf_counter()
                await asyncio.sleep(0.25)
                spans.append((start, time.perf_counter()))

        await asyncio.gather(*(copy(i) for i in range(3)))
        spans.sort()
        assert len(spans) == 3
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            assert s2 >= e1 - 0.01, "two copies were inside the line step at the same time"
    asyncio.run(go())


def test_lock_is_given_back_after_an_error():
    async def go():
        try:
            async with _line_db_lock():
                raise RuntimeError("AWS said no")
        except RuntimeError:
            pass
        # if the lock leaked, this would hang - give it 5 s
        async def again():
            async with _line_db_lock():
                return True
        assert await asyncio.wait_for(again(), timeout=5)
    asyncio.run(go())
