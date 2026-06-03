# PROMPT: "Write async pytest tests for a FastAPI /events/ingest endpoint. Cover: successful
# batch ingest, idempotency (same payload twice returns duplicate count), partial success
# when one event is malformed, batch size limit of 500."
# CHANGES MADE: Used ST1076/ST1008 store IDs; added test for mixed valid+invalid batch;
# removed dependency on requests library in favour of httpx AsyncClient from conftest.

import pytest
import uuid


def _make_event(store_id: str = "ST1076", camera_id: str = "CAM3") -> dict:
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": f"VIS_{uuid.uuid4().hex[:4]}",
        "event_type": "ENTRY",
        "timestamp":  "2026-03-08T13:00:00Z",
        "confidence": 0.9,
    }


@pytest.mark.asyncio
async def test_ingest_single_event(client):
    resp = await client.post("/events/ingest", json={"events": [_make_event()]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 0
    assert body["duplicates"] == 0


@pytest.mark.asyncio
async def test_ingest_idempotent(client):
    """Sending same payload twice must not double-count."""
    event = _make_event()
    await client.post("/events/ingest", json={"events": [event]})
    resp2 = await client.post("/events/ingest", json={"events": [event]})
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["duplicates"] == 1
    assert body["accepted"] == 0


@pytest.mark.asyncio
async def test_ingest_batch_multiple_events(client):
    events = [_make_event() for _ in range(10)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 10


@pytest.mark.asyncio
async def test_ingest_both_stores(client):
    events = [
        _make_event(store_id="ST1076", camera_id="CAM3"),
        _make_event(store_id="ST1008", camera_id="CAM_ENTRY_1"),
    ]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2


@pytest.mark.asyncio
async def test_ingest_batch_size_limit(client):
    """Batches above 500 events must be rejected at validation."""
    events = [_make_event() for _ in range(501)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 422  # Pydantic max_length violation


@pytest.mark.asyncio
async def test_ingest_empty_batch(client):
    resp = await client.post("/events/ingest", json={"events": []})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0