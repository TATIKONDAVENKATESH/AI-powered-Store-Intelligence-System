from __future__ import annotations
import os, sys, uuid
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.metrics import compute_metrics

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


async def _insert_event(db, event_type, visitor_id, zone_id=None, dwell_ms=0,
                        is_staff=0, ts_offset_s=0):
    ts = (NOW + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO events (event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence, ingested_at)
        VALUES (:eid, :sid, 'CAM_TEST', :vid, :et, :ts, :zid, :dm, :is_s, 0.9, :ts)
    """), {"eid": str(uuid.uuid4()), "sid": STORE, "vid": visitor_id,
           "et": event_type, "ts": ts, "zid": zone_id, "dm": dwell_ms, "is_s": is_staff})


async def _insert_pos(db, ts_offset_s=0):
    ts = (NOW + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
        VALUES (:tid, :sid, :ts, 500.0)
    """), {"tid": str(uuid.uuid4()), "sid": STORE, "ts": ts})


# ── Zero state ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_empty_store(db):
    m = await compute_metrics(STORE, db)
    assert m.unique_visitors == 0
    assert m.conversion_rate == 0.0
    assert m.queue_depth == 0


# ── Unique visitors ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_unique_visitors(db):
    for i in range(5):
        await _insert_event(db, "ENTRY", f"VIS_{i}")
    await db.commit()
    m = await compute_metrics(STORE, db)
    assert m.unique_visitors == 5


# ── Staff excluded ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_staff_excluded(db):
    await _insert_event(db, "ENTRY", "VIS_CUST_1", is_staff=0)
    await _insert_event(db, "ENTRY", "VIS_STAFF_1", is_staff=1)
    await db.commit()
    m = await compute_metrics(STORE, db)
    assert m.unique_visitors == 1   # only customer counts


# ── Conversion rate ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_conversion_rate(db):
    # 4 visitors enter; 2 visit billing; 1 pos transaction within 5 min window
    for i in range(4):
        await _insert_event(db, "ENTRY", f"VIS_{i}")
    # VIS_0 and VIS_1 visit BILLING at t=0
    await _insert_event(db, "ZONE_ENTER", "VIS_0", zone_id="BILLING", ts_offset_s=0)
    await _insert_event(db, "ZONE_ENTER", "VIS_1", zone_id="BILLING", ts_offset_s=0)
    # POS transaction 60s after their billing visit → both converted
    await _insert_pos(db, ts_offset_s=60)
    await db.commit()
    m = await compute_metrics(STORE, db)
    # 2 converted / 4 visitors = 0.5
    assert m.conversion_rate == 0.5


# ── Queue depth ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_queue_depth(db):
    # 3 joined, 1 exited → depth = 2
    for i in range(3):
        await _insert_event(db, "BILLING_QUEUE_JOIN", f"VIS_Q{i}")
    await _insert_event(db, "EXIT", "VIS_Q0")
    await db.commit()
    m = await compute_metrics(STORE, db)
    assert m.queue_depth == 2


# ── Abandonment rate ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_abandonment_rate(db):
    # 4 joined, 2 abandoned
    for i in range(4):
        await _insert_event(db, "BILLING_QUEUE_JOIN", f"VIS_A{i}")
    for i in range(2):
        await _insert_event(db, "BILLING_QUEUE_ABANDON", f"VIS_A{i}")
    await db.commit()
    m = await compute_metrics(STORE, db)
    assert m.abandonment_rate == 0.5


# ── Dwell per zone ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_dwell_per_zone(db):
    await _insert_event(db, "ZONE_DWELL", "VIS_D1", zone_id="SKINCARE_TOP", dwell_ms=60000)
    await _insert_event(db, "ZONE_DWELL", "VIS_D2", zone_id="SKINCARE_TOP", dwell_ms=120000)
    await db.commit()
    m = await compute_metrics(STORE, db)
    zones = {z.zone_id: z for z in m.avg_dwell_per_zone}
    assert "SKINCARE_TOP" in zones
    assert zones["SKINCARE_TOP"].avg_dwell_seconds == pytest.approx(90.0, abs=1.0)