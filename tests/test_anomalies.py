# PROMPT: "Write pytest-asyncio tests for app/anomalies.py. Cover: no anomalies
# when thresholds not exceeded, BILLING_QUEUE_SPIKE triggers at correct depth,
# CONVERSION_DROP triggers when rate < 10%, DEAD_ZONE triggers for inactive zone,
# severity levels are correct."
# CHANGES MADE: Seeded DB directly; patched QUEUE_SPIKE_THRESHOLD to 2 to keep
# test data small; fixed DEAD_ZONE to use a window far in the past so no events
# appear within the 30-minute window.

from __future__ import annotations
import os, sys, uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.anomalies import compute_anomalies

STORE = "STORE_BLR_002"
NOW   = datetime.now(timezone.utc)
OLD   = NOW - timedelta(hours=2)   # timestamp outside 30-min dead-zone window


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


async def _ev(db, event_type, visitor_id, zone_id=None, is_staff=0, ts=None):
    t = (ts or NOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO events (event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence, ingested_at)
        VALUES (:eid, :sid, 'CAM_TEST', :vid, :et, :ts, :zid, 0, :is_s, 0.9, :ts)
    """), {"eid": str(uuid.uuid4()), "sid": STORE, "vid": visitor_id,
           "et": event_type, "ts": t, "zid": zone_id, "is_s": is_staff})


async def _pos(db, ts=None):
    t = (ts or NOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
        VALUES (:tid, :sid, :ts, 300.0)
    """), {"tid": str(uuid.uuid4()), "sid": STORE, "ts": t})


# ── No anomalies ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_anomalies_empty_store(db):
    r = await compute_anomalies(STORE, db)
    # Empty store: no queue spike, no conversion drop (0 visitors → rate not evaluated),
    # but dead zones will fire because no visits at all.
    queue_spikes = [a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
    conversion_drops = [a for a in r.anomalies if a.anomaly_type == "CONVERSION_DROP"]
    assert len(queue_spikes) == 0
    assert len(conversion_drops) == 0


# ── BILLING_QUEUE_SPIKE ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_billing_queue_spike_triggers(db):
    # Patch threshold to 2 so we only need 3 visitors in queue
    with patch("app.anomalies.QUEUE_SPIKE_THRESHOLD", 2):
        for i in range(3):
            await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_Q{i}")
        await db.commit()
        r = await compute_anomalies(STORE, db)

    spikes = [a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
    assert len(spikes) == 1
    assert spikes[0].severity in ("WARN", "CRITICAL")


@pytest.mark.asyncio
async def test_billing_queue_spike_no_trigger_below_threshold(db):
    with patch("app.anomalies.QUEUE_SPIKE_THRESHOLD", 5):
        for i in range(2):
            await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_BQ{i}")
        await db.commit()
        r = await compute_anomalies(STORE, db)

    spikes = [a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"]
    assert len(spikes) == 0


# ── CONVERSION_DROP ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversion_drop_triggers(db):
    # 20 visitors, 0 purchases → conversion = 0% < 10% threshold
    for i in range(20):
        await _ev(db, "ENTRY", f"VIS_CD{i}")
        await _ev(db, "ZONE_ENTER", f"VIS_CD{i}", zone_id="BILLING")
    # No POS transactions → 0 conversions
    await db.commit()
    r = await compute_anomalies(STORE, db)
    drops = [a for a in r.anomalies if a.anomaly_type == "CONVERSION_DROP"]
    assert len(drops) == 1
    assert drops[0].severity == "WARN"


@pytest.mark.asyncio
async def test_conversion_drop_no_trigger_when_healthy(db):
    # 4 visitors, 3 converted → 75% > 10%
    for i in range(4):
        await _ev(db, "ENTRY", f"VIS_H{i}")
        await _ev(db, "ZONE_ENTER", f"VIS_H{i}", zone_id="BILLING",
                  ts=NOW - timedelta(seconds=60))
    for _ in range(3):
        await _pos(db, ts=NOW)
    await db.commit()
    r = await compute_anomalies(STORE, db)
    drops = [a for a in r.anomalies if a.anomaly_type == "CONVERSION_DROP"]
    assert len(drops) == 0


# ── DEAD_ZONE ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dead_zone_triggers_for_inactive_zone(db):
    # Only visit SKINCARE_TOP but leave other product zones empty in last 30 min
    await _ev(db, "ZONE_ENTER", "VIS_DZ1", zone_id="SKINCARE_TOP",
              ts=NOW - timedelta(minutes=5))   # recent → active
    # All others (MAKEUP_CENTER, FRAGRANCE_NAIL, etc.) have no recent events
    await db.commit()
    r = await compute_anomalies(STORE, db)
    dead = [a for a in r.anomalies if a.anomaly_type == "DEAD_ZONE"]
    # At minimum MAKEUP_CENTER, FRAGRANCE_NAIL, SKINCARE_BOTTOM, BILLING should be dead
    dead_zone_ids = {a.description.split("'")[1] for a in dead}
    assert "MAKEUP_CENTER" in dead_zone_ids


@pytest.mark.asyncio
async def test_dead_zone_severity_is_info(db):
    await db.commit()
    r = await compute_anomalies(STORE, db)
    dead = [a for a in r.anomalies if a.anomaly_type == "DEAD_ZONE"]
    assert all(a.severity == "INFO" for a in dead)