# conftest.py

from __future__ import annotations

import os
import sys

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ------------------------------------------------------------------
# Build test DB BEFORE importing app.main
# ------------------------------------------------------------------

TEST_ENGINE = create_async_engine(
    "sqlite+aiosqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    echo=False,
)

TEST_SESSION = async_sessionmaker(
    TEST_ENGINE,
    expire_on_commit=False,
)

import app.main as main_mod

# Patch app to use test DB
main_mod.engine = TEST_ENGINE
main_mod.SessionLocal = TEST_SESSION

_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "storage",
    "schema.sql",
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _apply_schema() -> None:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = f.read()

    async with TEST_ENGINE.begin() as conn:
        for stmt in schema.split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(text(stmt))


async def _clear_tables() -> None:
    async with TEST_ENGINE.begin() as conn:
        try:
            await conn.execute(text("DELETE FROM events"))
        except Exception:
            pass

        try:
            await conn.execute(text("DELETE FROM pos_transactions"))
        except Exception:
            pass


# ------------------------------------------------------------------
# Runs before EVERY test
# ------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    await _apply_schema()
    await _clear_tables()

    yield

    await _clear_tables()


# ------------------------------------------------------------------
# Shared DB session
# ------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_session():
    async with TEST_SESSION() as session:
        yield session


# ------------------------------------------------------------------
# FastAPI client
# ------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=main_mod.app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c