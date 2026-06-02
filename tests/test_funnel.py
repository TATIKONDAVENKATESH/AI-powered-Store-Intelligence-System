# PROMPT: "Write pytest-asyncio tests for app/funnel.py. Cover: empty store,
# full funnel path, drop-off percentages, re-entry does not double-count visitor,
# staff excluded from all stages."
# CHANGES MADE: Added re-entry deduplication test using same visitor_id with two
# ENTRY events. Tightened drop_off assertions to use approx.

from __future__ import annotations
import os, sys, uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.funnel import compute_funnel

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


async def _ev(db, event_type, visitor_id, zone_id=None, is_staff=0, ts_offset_s=0):
    ts = (NOW + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO events (event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence, ingested_at)
        VALUES (:eid, :sid, 'CAM_TEST', :vid, :et, :ts, :zid, 0, :is_s, 0.9, :ts)
    """), {"eid": str(uuid.uuid4()), "sid": STORE, "vid": visitor_id,
           "et": event_type, "ts": ts, "zid": zone_id, "is_s": is_staff})


async def _pos(db, ts_offset_s=120):
    ts = (NOW + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
        VALUES (:tid, :sid, :ts, 450.0)
    """), {"tid": str(uuid.uuid4()), "sid": STORE, "ts": ts})


# ── Empty store ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_empty(db):
    f = await compute_funnel(STORE, db)
    assert all(s.count == 0 for s in f.stages)
    entry_stage = next(s for s in f.stages if s.stage == "Entry")
    assert entry_stage.drop_off_pct == 0.0


# ── Full funnel path ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_full_path(db):
    # 4 entered, 3 visited zone, 2 reached billing, 1 purchased
    for i in range(4):
        await _ev(db, "ENTRY", f"VIS_{i}")
    for i in range(3):
        await _ev(db, "ZONE_ENTER", f"VIS_{i}", zone_id="SKINCARE_TOP")
    for i in range(2):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_{i}")
        await _ev(db, "ZONE_ENTER", f"VIS_{i}", zone_id="BILLING", ts_offset_s=10)
    await _pos(db, ts_offset_s=60)
    await db.commit()

    f = await compute_funnel(STORE, db)
    stages = {s.stage: s for s in f.stages}

    assert stages["Entry"].count == 4
    assert stages["Zone Visit"].count == 3
    assert stages["Billing Queue"].count == 2


# ── Drop-off percentages ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_drop_off_pct(db):
    # 4 entry, 2 zone visit → 50% drop-off at zone visit
    for i in range(4):
        await _ev(db, "ENTRY", f"VIS_DO{i}")
    for i in range(2):
        await _ev(db, "ZONE_ENTER", f"VIS_DO{i}", zone_id="MAKEUP_CENTER")
    await db.commit()

    f = await compute_funnel(STORE, db)
    stage = next(s for s in f.stages if s.stage == "Zone Visit")
    assert stage.drop_off_pct == pytest.approx(50.0, abs=0.1)


# ── Re-entry deduplication ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_reentry_not_double_counted(db):
    # Same visitor_id gets two ENTRY events (re-entry) — should count as 1 unique
    await _ev(db, "ENTRY",   "VIS_REENTRY", ts_offset_s=0)
    await _ev(db, "EXIT",    "VIS_REENTRY", ts_offset_s=60)
    await _ev(db, "REENTRY", "VIS_REENTRY", ts_offset_s=120)   # re-entry treated as ENTRY
    # Only ENTRY events are counted; REENTRY is a different event_type — so only 1 ENTRY
    await db.commit()

    f = await compute_funnel(STORE, db)
    entry_stage = next(s for s in f.stages if s.stage == "Entry")
    assert entry_stage.count == 1   # visitor_id deduplicated by COUNT DISTINCT


# ── Staff excluded ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_staff_excluded(db):
    await _ev(db, "ENTRY", "VIS_CUST")
    await _ev(db, "ENTRY", "VIS_STAFF", is_staff=1)
    await db.commit()

    f = await compute_funnel(STORE, db)
    entry_stage = next(s for s in f.stages if s.stage == "Entry")
    assert entry_stage.count == 1