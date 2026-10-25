"""Several test files run the app's real database code, each on its own event loop, in one pytest
process. A pooled connection opened on one loop can't be used from the next ("cannot perform
operation: another operation is in progress"), so a full run failed tests that pass on their own.
After each test the app engine starts a fresh pool, so no connection outlives the loop that opened
it (close=False: the old loop may already be gone). The app itself is unchanged."""
import pytest

import app.database as _db


@pytest.fixture(autouse=True)
def _fresh_db_pool_per_test():
    yield
    _db.engine.sync_engine.dispose(close=False)
