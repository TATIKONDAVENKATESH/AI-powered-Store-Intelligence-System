"""
test_heatmap.py — Tests for GET /stores/{store_id}/heatmap

Production behaviour (app/heatmap.py):
  - Queries ZONE_ENTER and ZONE_DWELL events with zone_id IS NOT NULL and is_staff=0
  - Groups by (zone_id, sku_zone)
  - visit_frequency = COUNT DISTINCT visitor_id per group
  - avg_dwell_seconds = AVG(dwell_ms) / 1000 per group
  - normalised_score = (visit_freq / max_visit_freq) * 100, rounded to 2dp
  - data_confidence = True if visit_frequency >= 20
  - Result is sorted by normalised_score descending
  - Empty store → zones=[]

Key correctness checks:
  - Staff events excluded
  - Normalisation: highest-frequency zone always scores 100.0
  - data_confidence threshold is exactly 20 (< 20 → False)
  - Zone with zero visits not included (only zones that have events)
"""
from __future__ import annotations

import os
import sys
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.heatmap import compute_heatmap

STORE = "ST1076"

_SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    with open(_SCHEMA_PATH) as f:
        schema = f.read()
    async with engine.begin() as conn:
        for stmt in schema.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


# ── Insert helper ─────────────────────────────────────────────────────────────

async def _ev(
    db,
    event_type: str,
    visitor_id: str,
    zone_id: str,
    dwell_ms: int = 0,
    is_staff: int = 0,
    sku_zone: str = None,
    store_id: str = STORE,
) -> None:
    await db.execute(text("""
        INSERT INTO events
          (event_id, store_id, camera_id, visitor_id, event_type,
           timestamp, zone_id, dwell_ms, is_staff, confidence,
           queue_depth, sku_zone, session_seq, ingested_at)
        VALUES
          (:eid, :sid, 'CAM_HM', :vid, :et,
           '2026-04-10T10:00:00Z', :zid, :dwell, :is_s, 0.9,
           NULL, :sku, 0, '2026-04-10T10:00:00Z')
    """), {
        "eid":   str(uuid.uuid4()),
        "sid":   store_id,
        "vid":   visitor_id,
        "et":    event_type,
        "zid":   zone_id,
        "dwell": dwell_ms,
        "is_s":  is_staff,
        "sku":   sku_zone,
    })


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heatmap_empty_store(db):
    """Empty store → zones=[] (no crash, no error)."""
    h = await compute_heatmap(STORE, db)
    assert h.store_id == STORE
    assert h.zones == []


@pytest.mark.asyncio
async def test_heatmap_response_structure(db):
    """HeatmapResponse must have store_id, zones, computed_at."""
    h = await compute_heatmap(STORE, db)
    assert hasattr(h, "store_id")
    assert hasattr(h, "zones")
    assert hasattr(h, "computed_at")


@pytest.mark.asyncio
async def test_heatmap_single_zone(db):
    """Single zone with 3 visitors → frequency=3, normalised=100.0."""
    for i in range(3):
        await _ev(db, "ZONE_ENTER", f"VIS_S{i}", zone_id="FRAGRANCES")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    assert len(h.zones) == 1
    z = h.zones[0]
    assert z.zone_id == "FRAGRANCES"
    assert z.visit_frequency == 3
    assert z.normalised_score == 100.0
    assert z.data_confidence is False  # 3 < 20


@pytest.mark.asyncio
async def test_heatmap_data_confidence_below_threshold(db):
    """data_confidence=False when visit_frequency < 20."""
    for i in range(19):
        await _ev(db, "ZONE_ENTER", f"VIS_CONF{i}", zone_id="ZONE_LO")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    assert zones["ZONE_LO"].data_confidence is False


@pytest.mark.asyncio
async def test_heatmap_data_confidence_at_threshold(db):
    """data_confidence=True when visit_frequency >= 20."""
    for i in range(20):
        await _ev(db, "ZONE_ENTER", f"VIS_HI{i}", zone_id="ZONE_HI")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    assert zones["ZONE_HI"].data_confidence is True


@pytest.mark.asyncio
async def test_heatmap_normalisation_two_zones(db):
    """
    Zone A: 10 visitors, Zone B: 5 visitors.
    Zone A normalised = 100.0, Zone B normalised = 50.0.
    """
    for i in range(10):
        await _ev(db, "ZONE_ENTER", f"VIS_A{i}", zone_id="ZONE_A")
    for i in range(5):
        await _ev(db, "ZONE_ENTER", f"VIS_B{i}", zone_id="ZONE_B")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    assert zones["ZONE_A"].normalised_score == 100.0
    assert abs(zones["ZONE_B"].normalised_score - 50.0) < 0.01


@pytest.mark.asyncio
async def test_heatmap_sorted_by_score_descending(db):
    """Zones must be ordered by normalised_score descending."""
    for i in range(8):
        await _ev(db, "ZONE_ENTER", f"VIS_H{i}", zone_id="HIGH_ZONE")
    for i in range(3):
        await _ev(db, "ZONE_ENTER", f"VIS_M{i}", zone_id="MID_ZONE")
    for i in range(1):
        await _ev(db, "ZONE_ENTER", f"VIS_L{i}", zone_id="LOW_ZONE")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    scores = [z.normalised_score for z in h.zones]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_heatmap_staff_excluded(db):
    """Staff events (is_staff=1) must not appear in zone frequency count."""
    await _ev(db, "ZONE_ENTER", "VIS_CUST",  zone_id="ZONE_X", is_staff=0)
    await _ev(db, "ZONE_ENTER", "VIS_STAFF", zone_id="ZONE_X", is_staff=1)
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    # Only 1 customer visit, not 2
    assert zones["ZONE_X"].visit_frequency == 1


@pytest.mark.asyncio
async def test_heatmap_zone_dwell_events_included(db):
    """ZONE_DWELL events also count toward zone frequency and dwell average."""
    await _ev(db, "ZONE_DWELL", "VIS_DW1", zone_id="ZONE_D", dwell_ms=60000)
    await _ev(db, "ZONE_DWELL", "VIS_DW2", zone_id="ZONE_D", dwell_ms=30000)
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    assert "ZONE_D" in zones
    assert zones["ZONE_D"].visit_frequency == 2
    # avg = (60000 + 30000) / 2 / 1000 = 45.0 seconds
    assert abs(zones["ZONE_D"].avg_dwell_seconds - 45.0) < 0.1


@pytest.mark.asyncio
async def test_heatmap_same_visitor_counted_once(db):
    """
    Same visitor_id with two ZONE_ENTER events in same zone → visit_frequency = 1.
    (COUNT DISTINCT visitor_id)
    """
    await _ev(db, "ZONE_ENTER", "VIS_SAME", zone_id="ZONE_Y")
    await _ev(db, "ZONE_ENTER", "VIS_SAME", zone_id="ZONE_Y")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    assert zones["ZONE_Y"].visit_frequency == 1


@pytest.mark.asyncio
async def test_heatmap_sku_zone_preserved(db):
    """sku_zone from the event must appear in the HeatmapZone response."""
    await _ev(db, "ZONE_ENTER", "VIS_SKU", zone_id="LIPSTICK_CENTER", sku_zone="LIPSTICK")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    zones = {z.zone_id: z for z in h.zones}
    assert zones["LIPSTICK_CENTER"].sku_zone == "LIPSTICK"


@pytest.mark.asyncio
async def test_heatmap_store_isolation(db):
    """Events from other stores must not appear in this store's heatmap."""
    await _ev(db, "ZONE_ENTER", "VIS_OTHER", zone_id="FOREIGN_ZONE", store_id="ST1008")
    await db.commit()

    h = await compute_heatmap(STORE, db)
    assert h.zones == []


@pytest.mark.asyncio
async def test_heatmap_zone_without_null_zone_id_excluded(db):
    """Events with zone_id=NULL must not appear in heatmap."""
    await db.execute(text("""
        INSERT INTO events
          (event_id, store_id, camera_id, visitor_id, event_type,
           timestamp, zone_id, dwell_ms, is_staff, confidence,
           queue_depth, sku_zone, session_seq, ingested_at)
        VALUES
          (:eid, :sid, 'CAM_HM', 'VIS_NULL_ZONE', 'ZONE_ENTER',
           '2026-04-10T10:00:00Z', NULL, 0, 0, 0.9,
           NULL, NULL, 0, '2026-04-10T10:00:00Z')
    """), {"eid": str(uuid.uuid4()), "sid": STORE})
    await db.commit()

    h = await compute_heatmap(STORE, db)
    assert h.zones == []


# ── HTTP endpoint tests (via conftest client) ──────────────────────────────────

@pytest.mark.asyncio
async def test_heatmap_endpoint_empty(client):
    resp = await client.get("/stores/ST1076/heatmap")
    assert resp.status_code == 200
    body = resp.json()
    assert "store_id" in body
    assert "zones" in body
    assert "computed_at" in body
    assert body["zones"] == []


@pytest.mark.asyncio
async def test_heatmap_endpoint_with_data(client, db_session):
    """Smoke test: ingest zone events and verify heatmap endpoint response structure."""
    for i in range(3):
        await db_session.execute(text("""
            INSERT INTO events
              (event_id, store_id, camera_id, visitor_id, event_type,
               timestamp, zone_id, dwell_ms, is_staff, confidence,
               queue_depth, sku_zone, session_seq, ingested_at)
            VALUES
              (:eid, 'ST1076', 'CAM_TEST', :vid, 'ZONE_ENTER',
               '2026-04-10T10:00:00Z', 'SKINCARE_TOP', 5000, 0, 0.9,
               NULL, 'SKINCARE', 0, '2026-04-10T10:00:00Z')
        """), {"eid": str(uuid.uuid4()), "vid": f"VIS_HT{i}"})
    await db_session.commit()

    resp = await client.get("/stores/ST1076/heatmap")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["zones"]) == 1
    z = body["zones"][0]
    assert z["zone_id"] == "SKINCARE_TOP"
    assert z["visit_frequency"] == 3
    assert z["normalised_score"] == 100.0
    assert z["data_confidence"] is False
    # Zone fields all present
    for field in ["zone_id", "sku_zone", "visit_frequency",
                  "avg_dwell_seconds", "normalised_score", "data_confidence"]:
        assert field in z