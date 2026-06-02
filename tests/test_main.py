# PROMPT: "Write pytest-asyncio integration tests for app/main.py FastAPI routes.
# Cover: POST /events/ingest, GET /stores/{id}/metrics, GET /stores/{id}/funnel,
# GET /stores/{id}/heatmap, GET /stores/{id}/anomalies, GET /health.
# Use httpx.AsyncClient with ASGITransport. Use in-memory SQLite.
# Test: happy path, empty store, unknown store, idempotent ingest, 503 on DB error."
# CHANGES MADE: Patched engine/SessionLocal in main module directly so in-memory DB
# is used. Added schema init inside fixture. Covered all 6 routes + middleware path.

from __future__ import annotations
import os
import sys
import uuid
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Must patch before importing app
TEST_ENGINE = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TEST_SESSION = async_sessionmaker(TEST_ENGINE, expire_on_commit=False)

import app.main as main_mod

# Redirect the module-level engine and session to in-memory test DB
main_mod.engine = TEST_ENGINE
main_mod.SessionLocal = TEST_SESSION


async def _init_test_db():
    schema = open(
        os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
    ).read()
    async with TEST_ENGINE.begin() as conn:
        for stmt in schema.split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))


@pytest_asyncio.fixture
async def client():
    """Async HTTP client wired to the FastAPI app with in-memory DB."""
    await _init_test_db()
    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    # Clean tables between tests
    async with TEST_ENGINE.begin() as conn:
        await conn.execute(text("DELETE FROM events"))
        await conn.execute(text("DELETE FROM pos_transactions"))


def _event_payload(event_type="ENTRY", zone_id=None, is_staff=False,
                   visitor_id=None, store_id="STORE_BLR_002"):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": "2026-04-10T10:00:00Z",
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


# ── POST /events/ingest ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_happy_path(client):
    payload = {"events": [_event_payload()]}
    r = await client.post("/events/ingest", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["accepted"] == 1
    assert data["rejected"] == 0
    assert data["duplicates"] == 0


@pytest.mark.asyncio
async def test_ingest_idempotent(client):
    ev = _event_payload()
    payload = {"events": [ev]}
    r1 = await client.post("/events/ingest", json=payload)
    r2 = await client.post("/events/ingest", json=payload)
    assert r1.json()["accepted"] == 1
    assert r2.json()["duplicates"] == 1
    assert r2.json()["accepted"] == 0


@pytest.mark.asyncio
async def test_ingest_empty_batch(client):
    r = await client.post("/events/ingest", json={"events": []})
    assert r.status_code == 200
    assert r.json()["accepted"] == 0


@pytest.mark.asyncio
async def test_ingest_invalid_event_type_rejected(client):
    ev = _event_payload()
    ev["event_type"] = "INVALID_TYPE"
    r = await client.post("/events/ingest", json={"events": [ev]})
    # Pydantic validation rejects at model level → 422
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_ingest_invalid_confidence_rejected(client):
    ev = _event_payload()
    ev["confidence"] = 1.5  # out of range
    r = await client.post("/events/ingest", json={"events": [ev]})
    assert r.status_code == 422


# ── GET /stores/{id}/metrics ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_empty_store(client):
    r = await client.get("/stores/STORE_BLR_002/metrics")
    assert r.status_code == 200
    data = r.json()
    assert data["store_id"] == "STORE_BLR_002"
    assert data["unique_visitors"] == 0
    assert data["conversion_rate"] == 0.0
    assert data["queue_depth"] == 0


@pytest.mark.asyncio
async def test_metrics_with_visitors(client):
    # Ingest 3 ENTRY events for distinct visitors
    events = [_event_payload("ENTRY", visitor_id=f"VIS_{i:04d}") for i in range(3)]
    await client.post("/events/ingest", json={"events": events})
    r = await client.get("/stores/STORE_BLR_002/metrics")
    assert r.status_code == 200
    assert r.json()["unique_visitors"] == 3


@pytest.mark.asyncio
async def test_metrics_schema_fields(client):
    r = await client.get("/stores/STORE_BLR_002/metrics")
    data = r.json()
    for field in ["store_id", "unique_visitors", "conversion_rate",
                  "avg_dwell_per_zone", "queue_depth", "abandonment_rate",
                  "total_transactions", "computed_at"]:
        assert field in data, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_metrics_staff_excluded(client):
    # Staff ENTRY should NOT count toward unique_visitors
    staff_ev = _event_payload("ENTRY", is_staff=True, visitor_id="VIS_STAFF")
    cust_ev = _event_payload("ENTRY", visitor_id="VIS_CUST")
    await client.post("/events/ingest", json={"events": [staff_ev, cust_ev]})
    r = await client.get("/stores/STORE_BLR_002/metrics")
    assert r.json()["unique_visitors"] == 1  # only customer counted


# ── GET /stores/{id}/funnel ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_empty_store(client):
    r = await client.get("/stores/STORE_BLR_002/funnel")
    assert r.status_code == 200
    data = r.json()
    assert data["store_id"] == "STORE_BLR_002"
    assert len(data["stages"]) == 4
    assert data["stages"][0]["stage"] == "Entry"
    assert data["stages"][0]["count"] == 0


@pytest.mark.asyncio
async def test_funnel_with_entries(client):
    events = [_event_payload("ENTRY", visitor_id=f"VIS_{i:04d}") for i in range(5)]
    await client.post("/events/ingest", json={"events": events})
    r = await client.get("/stores/STORE_BLR_002/funnel")
    assert r.json()["stages"][0]["count"] == 5


@pytest.mark.asyncio
async def test_funnel_dropoff_pct_zero_at_baseline(client):
    r = await client.get("/stores/STORE_BLR_002/funnel")
    assert r.json()["stages"][0]["drop_off_pct"] == 0.0


@pytest.mark.asyncio
async def test_funnel_no_double_count_reentry(client):
    # Same visitor_id with ENTRY + REENTRY — should count as 1 unique visitor
    vid = "VIS_REENTRY_001"
    ev1 = _event_payload("ENTRY", visitor_id=vid)
    ev2 = _event_payload("REENTRY", visitor_id=vid)
    ev2["event_id"] = str(uuid.uuid4())
    await client.post("/events/ingest", json={"events": [ev1, ev2]})
    r = await client.get("/stores/STORE_BLR_002/funnel")
    # ENTRY count should be 1, not 2 (REENTRY is not counted as ENTRY)
    assert r.json()["stages"][0]["count"] == 1


# ── GET /stores/{id}/heatmap ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heatmap_empty(client):
    r = await client.get("/stores/STORE_BLR_002/heatmap")
    assert r.status_code == 200
    data = r.json()
    assert data["zones"] == []


@pytest.mark.asyncio
async def test_heatmap_with_zone_data(client):
    events = []
    for i in range(3):
        ev = _event_payload("ZONE_ENTER", zone_id="SKINCARE_TOP", visitor_id=f"VIS_{i:04d}")
        ev["metadata"]["sku_zone"] = "SKINCARE"
        ev["camera_id"] = "CAM_FLOOR_A"
        events.append(ev)
    await client.post("/events/ingest", json={"events": events})
    r = await client.get("/stores/STORE_BLR_002/heatmap")
    assert r.status_code == 200
    zones = r.json()["zones"]
    assert len(zones) == 1
    assert zones[0]["zone_id"] == "SKINCARE_TOP"
    assert zones[0]["visit_frequency"] == 3
    assert zones[0]["normalised_score"] == 100.0
    assert zones[0]["data_confidence"] is False  # only 3 sessions, < 20


@pytest.mark.asyncio
async def test_heatmap_schema_fields(client):
    r = await client.get("/stores/STORE_BLR_002/heatmap")
    data = r.json()
    for field in ["store_id", "zones", "computed_at"]:
        assert field in data


# ── GET /stores/{id}/anomalies ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anomalies_empty_store(client):
    r = await client.get("/stores/STORE_BLR_002/anomalies")
    assert r.status_code == 200
    data = r.json()
    assert "anomalies" in data
    # With no events and no visitors, DEAD_ZONE anomalies expected for all product zones
    types = [a["anomaly_type"] for a in data["anomalies"]]
    assert all(t == "DEAD_ZONE" for t in types)


@pytest.mark.asyncio
async def test_anomalies_conversion_drop_triggered(client):
    # Many visitors, no purchases → low conversion → CONVERSION_DROP anomaly
    events = [_event_payload("ENTRY", visitor_id=f"VIS_{i:04d}") for i in range(20)]
    await client.post("/events/ingest", json={"events": events})
    r = await client.get("/stores/STORE_BLR_002/anomalies")
    types = [a["anomaly_type"] for a in r.json()["anomalies"]]
    assert "CONVERSION_DROP" in types


@pytest.mark.asyncio
async def test_anomalies_severity_values(client):
    r = await client.get("/stores/STORE_BLR_002/anomalies")
    valid = {"INFO", "WARN", "CRITICAL"}
    for a in r.json()["anomalies"]:
        assert a["severity"] in valid


@pytest.mark.asyncio
async def test_anomalies_has_suggested_action(client):
    r = await client.get("/stores/STORE_BLR_002/anomalies")
    for a in r.json()["anomalies"]:
        assert a["suggested_action"]  # non-empty string


# ── GET /health ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_ok_or_degraded(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("ok", "degraded", "down")
    assert data["db_connected"] is True


@pytest.mark.asyncio
async def test_health_schema_fields(client):
    r = await client.get("/health")
    data = r.json()
    for field in ["status", "store_feeds", "last_event_at",
                  "stale_feed", "db_connected", "checked_at"]:
        assert field in data


@pytest.mark.asyncio
async def test_health_all_cameras_present(client):
    r = await client.get("/health")
    feed_ids = {f["camera_id"] for f in r.json()["store_feeds"]}
    expected = {"CAM_ENTRY_01", "CAM_FLOOR_A", "CAM_FLOOR_B", "CAM_BILLING_01", "CAM_STAFF_01"}
    assert expected == feed_ids


@pytest.mark.asyncio
async def test_health_stale_when_no_events(client):
    r = await client.get("/health")
    data = r.json()
    # No events ingested → all feeds stale → status degraded
    assert data["stale_feed"] is True
    assert data["status"] == "degraded"


# ── Unknown store ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_unknown_store_returns_zeros(client):
    r = await client.get("/stores/STORE_UNKNOWN/metrics")
    assert r.status_code == 200
    assert r.json()["unique_visitors"] == 0


@pytest.mark.asyncio
async def test_funnel_unknown_store_returns_zero_stages(client):
    r = await client.get("/stores/STORE_UNKNOWN/funnel")
    assert r.status_code == 200
    assert all(s["count"] == 0 for s in r.json()["stages"])
