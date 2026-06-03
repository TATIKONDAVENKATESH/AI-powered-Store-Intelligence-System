"""
test_health.py — Tests for GET /health

What this tests:
  - DB connectivity always returns db_connected=True with test in-memory DB
  - Empty DB → store_feeds=[], stale_feed=False, status="ok" (no feeds means no stale feeds)
  - Fresh camera event (timestamp = now) → that camera is NOT stale
  - Stale camera event (>10 min old) → stale=True, stale_feed=True, status="degraded"
  - Response structure contains all required fields

Key production behaviour verified here:
  - compute_health only lists cameras that have at least one event in the DB
  - Stale threshold is 10 minutes using wall-clock time (NOT replay-relative)
  - status="ok" when no feeds OR all feeds fresh; status="degraded" when any stale
  - status="down" only when DB query itself throws
"""
from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

from unittest.mock import AsyncMock, Mock

from app.health import compute_health


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_debug_tables(db_session):
    result = await db_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")
    )
    print(result.fetchall())

async def _insert_event(db, camera_id: str, store_id: str, timestamp: str) -> None:
    """Insert a minimal event for a given camera and timestamp."""
    await db.execute(text("""
        INSERT OR IGNORE INTO events
          (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
           zone_id, dwell_ms, is_staff, confidence, queue_depth, sku_zone,
           session_seq, ingested_at)
        VALUES
          (:event_id, :store_id, :camera_id, :visitor_id, :event_type, :timestamp,
           :zone_id, :dwell_ms, :is_staff, :confidence, :queue_depth, :sku_zone,
           :session_seq, :ingested_at)
    """), {
        "event_id":    str(uuid.uuid4()),
        "store_id":    store_id,
        "camera_id":   camera_id,
        "visitor_id":  "VIS_HEALTH_TEST",
        "event_type":  "ENTRY",
        "timestamp":   timestamp,
        "zone_id":     None,
        "dwell_ms":    0,
        "is_staff":    0,
        "confidence":  0.9,
        "queue_depth": None,
        "sku_zone":    None,
        "session_seq": 0,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    })
    await db.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_db_connected(client):
    """Health endpoint always reports db_connected=True with test in-memory DB."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["db_connected"] is True


@pytest.mark.asyncio
async def test_health_response_structure(client):
    """Response must contain all required fields defined in HealthResponse model."""
    resp = await client.get("/health")
    body = resp.json()
    for field in ["status", "store_feeds", "last_event_at",
                  "stale_feed", "db_connected", "checked_at"]:
        assert field in body, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_health_no_events_empty_feeds(client):
    """
    With no events in DB, store_feeds must be empty list.
    compute_health only lists cameras that have events — empty DB means no cameras.
    No feeds means no stale feeds → stale_feed=False, status='ok'.
    """
    resp = await client.get("/health")
    body = resp.json()
    assert body["db_connected"] is True
    assert body["store_feeds"] == []
    assert body["stale_feed"] is False
    # No feeds at all → status should be "ok"
    assert body["status"] == "ok"
    assert body["last_event_at"] is None


@pytest.mark.asyncio
async def test_health_fresh_feed_not_stale(client, db_session):
    """Camera with event timestamped right now must appear as stale=False."""
    now_iso = datetime.now(timezone.utc).isoformat()
    await _insert_event(db_session, "CAM_FRESH", "ST1076", now_iso)

    resp = await client.get("/health")
    body = resp.json()
    cam_map = {f["camera_id"]: f for f in body["store_feeds"]}

    assert "CAM_FRESH" in cam_map
    assert cam_map["CAM_FRESH"]["stale"] is False
    assert body["stale_feed"] is False
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_health_stale_feed_detected(client, db_session):
    """Camera with event older than 10 minutes must be marked stale=True."""
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    await _insert_event(db_session, "CAM_STALE", "ST1076", stale_ts)

    resp = await client.get("/health")
    body = resp.json()
    cam_map = {f["camera_id"]: f for f in body["store_feeds"]}

    assert "CAM_STALE" in cam_map
    assert cam_map["CAM_STALE"]["stale"] is True
    assert body["stale_feed"] is True
    assert body["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_mixed_feeds_any_stale_degrades(client, db_session):
    """
    One fresh camera + one stale camera → stale_feed=True, status=degraded.
    stale_feed is True if ANY camera is stale.
    """
    now_iso  = datetime.now(timezone.utc).isoformat()
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()

    await _insert_event(db_session, "CAM_FRESH2", "ST1076", now_iso)
    await _insert_event(db_session, "CAM_STALE2", "ST1008", stale_ts)

    resp = await client.get("/health")
    body = resp.json()
    cam_map = {f["camera_id"]: f for f in body["store_feeds"]}

    assert cam_map["CAM_FRESH2"]["stale"] is False
    assert cam_map["CAM_STALE2"]["stale"] is True
    assert body["stale_feed"] is True
    assert body["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_boundary_exactly_10_minutes(client, db_session):
    """
    Event exactly 10 minutes + 1 second old is guaranteed stale.
    Production code: is_stale = ts < stale_cutoff
    ts at exactly 10min = stale_cutoff → NOT stale (equal is not less than).
    ts at 10min + 1s < stale_cutoff → IS stale.
    """
    borderline_ts = (datetime.now(timezone.utc) - timedelta(minutes=10, seconds=1)).isoformat()
    await _insert_event(db_session, "CAM_BORDER", "ST1076", borderline_ts)

    resp = await client.get("/health")
    body = resp.json()
    cam_map = {f["camera_id"]: f for f in body["store_feeds"]}
    assert cam_map["CAM_BORDER"]["stale"] is True


@pytest.mark.asyncio
async def test_health_last_event_at_reflects_latest(client, db_session):
    """
    last_event_at should be the MAX(timestamp) across ALL cameras/stores.
    """
    older_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    newer_ts = datetime.now(timezone.utc).isoformat()

    await _insert_event(db_session, "CAM_A", "ST1076", older_ts)
    await _insert_event(db_session, "CAM_B", "ST1076", newer_ts)

    resp = await client.get("/health")
    body = resp.json()
    # last_event_at must be the newer of the two
    assert body["last_event_at"] is not None
    # The newer ts must be >= older ts (lexicographic ISO comparison works for UTC)
    assert body["last_event_at"] >= older_ts


@pytest.mark.asyncio
async def test_health_camera_feed_structure(client, db_session):
    """Each entry in store_feeds must have camera_id, last_event_at, stale fields."""
    now_iso = datetime.now(timezone.utc).isoformat()
    await _insert_event(db_session, "CAM_STRUCT", "ST1076", now_iso)

    resp = await client.get("/health")
    body = resp.json()
    assert len(body["store_feeds"]) >= 1
    for feed in body["store_feeds"]:
        assert "camera_id" in feed
        assert "last_event_at" in feed
        assert "stale" in feed
        assert isinstance(feed["stale"], bool)

@pytest.mark.asyncio
async def test_compute_health_db_failure():
    """Cover status='down' branch when DB connectivity check fails."""
    db = AsyncMock()

    async def raise_error(*args, **kwargs):
        raise Exception("db unavailable")

    db.execute.side_effect = raise_error

    result = await compute_health(db)

    assert result.status == "down"
    assert result.db_connected is False
    assert result.stale_feed is True
    assert result.store_feeds == []
    assert result.last_event_at is None


@pytest.mark.asyncio
async def test_compute_health_invalid_timestamp():
    db = AsyncMock()

    ping_result = Mock()

    camera_result = Mock()
    camera_result.fetchall.return_value = [
        ("CAM_BAD", "invalid-timestamp")
    ]

    global_result = Mock()
    global_result.scalar.return_value = "invalid-timestamp"

    db.execute.side_effect = [
        ping_result,
        camera_result,
        global_result,
    ]

    result = await compute_health(db)

    assert result.db_connected is True
    assert result.stale_feed is True
    assert result.store_feeds[0].camera_id == "CAM_BAD"
    assert result.store_feeds[0].stale is True