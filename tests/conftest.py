"""Shared test setup. pytest imports this before any test module.

Safety first: tests must never touch the dev or production database. The app
connects to DATABASE_URL, so it is removed here and replaced only by
TEST_DATABASE_URL when that is set. Without it, database tests are skipped.
"""

import os

test_url = os.environ.pop("TEST_DATABASE_URL", None)
os.environ.pop("DATABASE_URL", None)
if test_url:
    os.environ["DATABASE_URL"] = test_url

# These imports read DATABASE_URL, so they must come after the lines above.
import pytest
from fastapi.testclient import TestClient

from api.db import Base, engine
from api.index import app


@pytest.fixture(scope="session")
def client():
    """App client for tests that never reach the database."""
    return TestClient(app)


@pytest.fixture(scope="session")
def db_client(client):
    """App client for tests that need the database; skips without one."""
    if engine is None:
        pytest.skip("TEST_DATABASE_URL not set")
    Base.metadata.create_all(engine)  # only creates missing tables
    return client
