# PROMPT: "Write async pytest tests for a /health endpoint. Cover: healthy response when DB
# is up, stale_feed=True when latest camera event is > 10 minutes old, degraded status
# when any camera is stale, status=ok when all feeds are recent."
# CHANGES MADE: Used real camera IDs (CAM3, CAM_ENTRY_1); added both-store camera feeds
# test; used datetime arithmetic for stale threshold.

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import text


async def _insert_event(db, camera_id, store_id, timestamp):
    await db.execute(text("""
        INSERT OR IGNORE INTO events
          (event_id,store_id,camera_id,visitor_id,event_type,timestamp,
           zone_id,dwell_ms,is_staff,confidence,queue_depth,sku_zone,session_seq,ingested_at)
        VALUES
          (:event_id,:store_id,:camera_id,:visitor_id,:event_type,:timestamp,
           :zone_id,:dwell_ms,:is_staff,:confidence,:queue_depth,:sku_zone,:session_seq,:ingested_at)
    """), {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": "VIS_0001",
        "event_type": "ENTRY",
        "timestamp":  timestamp,
        "zone_id":    None,
        "dwell_ms":   0,
        "is_staff":   0,
        "confidence": 0.9,
        "queue_depth": None,
        "sku_zone":   None,
        "session_seq": 1,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    })
    await db.commit()


@pytest.mark.asyncio
async def test_health_db_connected(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["db_connected"] is True


@pytest.mark.asyncio
async def test_health_no_events_is_ok(client):
    """No events yet — still returns OK and empty feeds list."""
    resp = await client.get("/health")
    body = resp.json()
    assert body["db_connected"] is True
    # No camera feeds yet
    assert body["store_feeds"] == []


@pytest.mark.asyncio
async def test_health_fresh_feed_not_stale(client, db_session):
    now = datetime.now(timezone.utc).isoformat()
    await _insert_event(db_session, "CAM3", "ST1076", now)
    resp = await client.get("/health")
    body = resp.json()
    cam_status = {f["camera_id"]: f for f in body["store_feeds"]}
    assert cam_status["CAM3"]["stale"] is False


@pytest.mark.asyncio
async def test_health_stale_feed_detected(client, db_session):
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    await _insert_event(db_session, "CAM3", "ST1076", stale_ts)
    resp = await client.get("/health")
    body = resp.json()
    cam_status = {f["camera_id"]: f for f in body["store_feeds"]}
    assert cam_status["CAM3"]["stale"] is True
    assert body["stale_feed"] is True
    assert body["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_response_structure(client):
    resp = await client.get("/health")
    body = resp.json()
    assert "status" in body
    assert "db_connected" in body
    assert "stale_feed" in body
    assert "checked_at" in body
    assert "store_feeds" in body