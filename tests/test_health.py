# PROMPT: "Write pytest-asyncio tests for app/health.py. Cover: status ok when
# all cameras fresh, status degraded when any camera stale, status down when DB
# unreachable, stale_feed flag, per-camera last_event_at populated."
# CHANGES MADE: Simulated DB failure by patching db.execute to raise. Added check
# that all expected camera IDs appear in response.

from __future__ import annotations
import os, sys, uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.health import compute_health, ALL_CAMERAS

STORE = "STORE_BLR_002"
NOW   = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    schema = open(os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")).read()
    async with engine.begin() as conn:
        for stmt in schema.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


async def _ev(db, camera_id, ts):
    await db.execute(text("""
        INSERT INTO events (event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence, ingested_at)
        VALUES (:eid, 'STORE_BLR_002', :cam, 'VIS_H1', 'ENTRY', :ts, NULL, 0, 0, 0.9, :ts)
    """), {"eid": str(uuid.uuid4()), "cam": camera_id, "ts": ts})


# ── All cameras fresh → ok ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_ok_when_all_fresh(db):
    # Insert a recent event for every camera
    fresh_ts = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cam in ALL_CAMERAS:
        await _ev(db, cam, fresh_ts)
    await db.commit()

    h = await compute_health(db)
    assert h.status == "ok"
    assert h.db_connected is True
    assert h.stale_feed is False


# ── Any stale camera → degraded ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_degraded_when_one_stale(db):
    # Fresh events for all except one camera
    fresh_ts = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for cam in ALL_CAMERAS[1:]:
        await _ev(db, cam, fresh_ts)
    # ALL_CAMERAS[0] gets no event → stale
    await db.commit()

    h = await compute_health(db)
    assert h.status == "degraded"
    assert h.stale_feed is True


# ── No events at all → degraded (all stale) ───────────────────────────────────

@pytest.mark.asyncio
async def test_health_all_stale_when_no_events(db):
    h = await compute_health(db)
    assert h.status == "degraded"
    stale_cams = [f for f in h.store_feeds if f.stale]
    assert len(stale_cams) == len(ALL_CAMERAS)


# ── DB failure → down ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_down_when_db_fails():
    bad_db = MagicMock()
    bad_db.execute = AsyncMock(side_effect=Exception("DB connection lost"))

    h = await compute_health(bad_db)
    assert h.status == "down"
    assert h.db_connected is False
    assert all(f.stale for f in h.store_feeds)


# ── All camera IDs present in response ───────────────────────────────────────

@pytest.mark.asyncio
async def test_health_all_cameras_in_response(db):
    h = await compute_health(db)
    response_cams = {f.camera_id for f in h.store_feeds}
    assert set(ALL_CAMERAS) == response_cams


# ── last_event_at is populated ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_last_event_at_populated(db):
    ts = (NOW - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await _ev(db, ALL_CAMERAS[0], ts)
    await db.commit()
    h = await compute_health(db)
    assert h.last_event_at is not None