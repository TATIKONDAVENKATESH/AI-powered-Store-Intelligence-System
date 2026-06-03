# PROMPT: "Write a pytest conftest.py that provides async fixtures for FastAPI testing with
# in-memory SQLite. Need: a 'client' fixture (httpx AsyncClient bound to the app),
# a 'db_session' fixture (same in-memory DB the client uses), both async, both reset
# between tests."
# CHANGES MADE: Patched main_mod engine/session BEFORE importing routes so schema SQL
# path resolves correctly; used absolute path for schema.sql; added db_session fixture
# that shares the same engine so inserts via db_session are visible to the client.

from __future__ import annotations
import os
import sys
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Build in-memory engine BEFORE importing app.main so the patch takes effect
TEST_ENGINE  = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TEST_SESSION = async_sessionmaker(TEST_ENGINE, expire_on_commit=False)

import app.main as main_mod

# Redirect module-level engine and session to in-memory test DB
main_mod.engine       = TEST_ENGINE
main_mod.SessionLocal = TEST_SESSION

_SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
)


async def _apply_schema():
    """Apply the real schema SQL to the in-memory test DB."""
    with open(_SCHEMA_PATH, "r") as f:
        schema = f.read()
    async with TEST_ENGINE.begin() as conn:
        for stmt in schema.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))


async def _clear_tables():
    """Delete all rows between tests to prevent state bleed."""
    async with TEST_ENGINE.begin() as conn:
        await conn.execute(text("DELETE FROM events"))
        await conn.execute(text("DELETE FROM pos_transactions"))


@pytest_asyncio.fixture
async def db_session():
    """
    Async DB session using the same in-memory engine as the test client.
    Inserts made via this fixture are immediately visible to API endpoints.
    """
    await _apply_schema()
    async with TEST_SESSION() as session:
        yield session
    await _clear_tables()


@pytest_asyncio.fixture
async def client(db_session):
    """
    Async HTTP client wired to the FastAPI app with in-memory SQLite.
    Depends on db_session so schema is applied and tables are cleared per test.
    """
    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c