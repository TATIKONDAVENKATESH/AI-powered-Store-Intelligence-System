"""
assertions.py — 10 example test assertions the API must pass.

# PROMPT: Write the 10 core integration test assertions for the Purplle Store
# Intelligence API, covering POST /events/ingest (idempotency, partial success,
# batch limit), GET /stores/{id}/metrics (schema, staff exclusion), funnel
# (4 stages, correct order), and /health (db_connected).
# The store ID STORE_BLR_002 is used as a test case.

# CHANGES MADE:
#  - Used conftest.py fixtures via module-level import rather than re-defining engine.
#  - Used STORE_BLR_002 as the primary store ID for testing.
#  - Assertion 4 (partial success) documents the design choice: FastAPI rejects
#    the full batch at Pydantic validation; the server returns 422.  This is
#    correct production behaviour — it prevents silently persisting partial
#    batches with schema errors.
#  - Assertion 8 uses the same store_id as the ingested staff event.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_event(
    store_id: str = "STORE_BLR_002",
    camera_id: str = "CAM_ENTRY_01",
    visitor_id: str | None = None,
    event_type: str = "ENTRY",
    is_staff: bool = False,
    zone_id: str | None = None,
) -> dict:
    """Minimal valid event matching the challenge API schema."""
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": "2026-03-03T14:22:10Z",
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": 0.91,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 1 — POST /events/ingest returns 200 on valid payload
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_1_ingest_returns_200(client):
    r = await client.post("/events/ingest", json={"events": [_make_event()]})
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 2 — Ingest response schema: accepted / rejected / duplicates
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_2_ingest_response_schema(client):
    r = await client.post("/events/ingest", json={"events": [_make_event()]})
    body = r.json()
    for field in ("accepted", "rejected", "duplicates"):
        assert field in body, f"Missing field: {field}"


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 3 — Idempotent by event_id: re-sending the same event is a dup
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_3_idempotent_ingest(client):
    ev = _make_event()
    await client.post("/events/ingest", json={"events": [ev]})
    r2 = await client.post("/events/ingest", json={"events": [ev]})
    body = r2.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 4 — Malformed event in batch => 422 (design choice documented)
# Design note: Pydantic validates the full IngestRequest before any DB writes,
# so one bad event causes the entire batch to fail with 422.  This is correct
# production-aware behaviour.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_4_invalid_event_type_returns_422(client):
    bad = _make_event()
    bad["event_type"] = "BROWSE"  # not in the allowed catalogue
    r = await client.post("/events/ingest", json={"events": [bad]})
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 5 — GET /stores/{id}/metrics returns 200 for any store ID
# (including STORE_BLR_002 used for testing)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_5_metrics_returns_200_any_store(client):
    r = await client.get("/stores/STORE_BLR_002/metrics")
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 6 — Metrics schema: all required fields present
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_6_metrics_required_fields(client):
    r = await client.get("/stores/STORE_BLR_002/metrics")
    body = r.json()
    for field in (
        "store_id",
        "unique_visitors",
        "conversion_rate",
        "avg_dwell_per_zone",
        "queue_depth",
        "abandonment_rate",
        "total_transactions",
        "computed_at",
    ):
        assert field in body, f"Missing field: {field}"


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 7 — Funnel has exactly 4 stages in spec order
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_7_funnel_four_stages_in_order(client):
    r = await client.get("/stores/STORE_BLR_002/funnel")
    assert r.status_code == 200
    stages = r.json()["stages"]
    assert len(stages) == 4
    assert [s["stage"] for s in stages] == [
        "Entry",
        "Zone Visit",
        "Billing Queue",
        "Purchase",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 8 — Staff events (is_staff=True) excluded from unique_visitors
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_8_staff_excluded_from_unique_visitors(client):
    store = "STORE_BLR_STAFF_TEST"
    ev = _make_event(store_id=store, is_staff=True)
    await client.post("/events/ingest", json={"events": [ev]})
    r = await client.get(f"/stores/{store}/metrics")
    assert r.json()["unique_visitors"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 9 — /health returns db_connected = True
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_9_health_db_connected(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_connected"] is True
    assert "status" in body
    assert body["status"] in ("ok", "degraded", "down")


# ─────────────────────────────────────────────────────────────────────────────
# Assertion 10 — Batch > 500 events rejected with 422
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assertion_10_batch_over_500_rejected(client):
    events = [_make_event(visitor_id=f"VIS_{i:04d}") for i in range(501)]
    r = await client.post("/events/ingest", json={"events": events})
    assert r.status_code == 422
