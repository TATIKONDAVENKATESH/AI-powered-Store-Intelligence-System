"""
test_anomalies.py — Tests for GET /stores/{store_id}/anomalies

Production behaviour (app/anomalies.py):

QUEUE SPIKE:
  - queue_depth = visitors who did BILLING_QUEUE_JOIN and have NOT exited or abandoned
  - WARN if queue_depth >= 3 (QUEUE_WARN_DEPTH)
  - CRITICAL if queue_depth >= 6 (QUEUE_CRITICAL_DEPTH)

CONVERSION DROP:
  - Only flagged if total_visitors > 10
  - WARN if conversion_rate < 0.10 (10%)
  - Conversion = visitor with BILLING zone ZONE_ENTER + POS transaction within 1800s

DEAD ZONE (critical fix):
  - Uses MAX(timestamp) from events AS reference point (replay-relative), NOT now()
  - cutoff = MAX(timestamp) - 30 minutes
  - dead_zones = all_zones (any ZONE_ENTER ever) - active_zones (ZONE_ENTER within cutoff)
  - Only non-BILLING zones included
  - CORRECT TEST PATTERN: insert a recent event to push MAX(timestamp) forward,
    so the old zone event falls outside the 30-minute active window

EMPTY STORE:
  - No events → no anomalies of any type (all_zones is empty → no DEAD_ZONE either)

All anomalies have: anomaly_type, severity, description, suggested_action, detected_at
"""
from __future__ import annotations

import os
import sys
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.anomalies import compute_anomalies

STORE = "ST1076"
_NOW  = datetime.now(timezone.utc)

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


# ── Insert helpers ────────────────────────────────────────────────────────────

def _ts(offset_minutes: int = 0) -> str:
    """Return ISO-8601 UTC string offset_minutes from _NOW."""
    return (_NOW + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _ev(
    db,
    event_type: str,
    visitor_id: str,
    zone_id: str = None,
    is_staff: int = 0,
    timestamp: str = None,
    store_id: str = STORE,
) -> None:
    ts = timestamp or _ts(0)
    await db.execute(text("""
        INSERT INTO events
          (event_id, store_id, camera_id, visitor_id, event_type,
           timestamp, zone_id, dwell_ms, is_staff, confidence,
           queue_depth, sku_zone, session_seq, ingested_at)
        VALUES
          (:eid, :sid, 'CAM_ANOM', :vid, :et,
           :ts, :zid, 0, :is_s, 0.9,
           NULL, NULL, 0, :ts)
    """), {
        "eid":  str(uuid.uuid4()),
        "sid":  store_id,
        "vid":  visitor_id,
        "et":   event_type,
        "ts":   ts,
        "zid":  zone_id,
        "is_s": is_staff,
    })


async def _pos(db, ts: str = None, store_id: str = STORE) -> None:
    await db.execute(text("""
        INSERT INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
        VALUES (:tid, :sid, :ts, 500.0)
    """), {"tid": str(uuid.uuid4()), "sid": store_id, "ts": ts or _ts(0)})


# ── Empty store tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_anomalies_empty_store(db):
    """
    Empty DB → no anomalies of any type.

    CORRECTION: all_zones is derived from ZONE_ENTER events. With no events,
    all_zones is empty → no DEAD_ZONE anomalies fire.  Queue is 0 (no spike).
    total_visitors = 0 which is not > 10 (no conversion drop).
    Result: anomalies == [].
    """
    r = await compute_anomalies(STORE, db)
    assert r.store_id == STORE
    assert r.anomalies == []


@pytest.mark.asyncio
async def test_anomaly_response_structure(db):
    """AnomalyResponse must have store_id, anomalies, computed_at."""
    r = await compute_anomalies(STORE, db)
    assert hasattr(r, "store_id")
    assert hasattr(r, "anomalies")
    assert hasattr(r, "computed_at")


# ── Queue spike tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_spike_below_warn_threshold_no_anomaly(db):
    """Queue depth 2 (< QUEUE_WARN_DEPTH=3) → no BILLING_QUEUE_SPIKE anomaly."""
    for i in range(2):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_BQ{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "BILLING_QUEUE_SPIKE" not in types


@pytest.mark.asyncio
async def test_queue_spike_warn_at_threshold(db):
    """Queue depth exactly 3 → BILLING_QUEUE_SPIKE with severity WARN."""
    for i in range(3):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_WQ{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    spike = next((a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike.severity == "WARN"


@pytest.mark.asyncio
async def test_queue_spike_warn_between_thresholds(db):
    """Queue depth 4 (>= 3 and < 6) → WARN."""
    for i in range(4):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_WM{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    spike = next((a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike.severity == "WARN"


@pytest.mark.asyncio
async def test_queue_spike_critical_at_threshold(db):
    """Queue depth exactly 6 (QUEUE_CRITICAL_DEPTH) → CRITICAL severity."""
    for i in range(6):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_CQ{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    spike = next((a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike.severity == "CRITICAL"


@pytest.mark.asyncio
async def test_queue_spike_critical_above_threshold(db):
    """Queue depth 7 → CRITICAL."""
    for i in range(7):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_CX{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    spike = next((a for a in r.anomalies if a.anomaly_type == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike.severity == "CRITICAL"


@pytest.mark.asyncio
async def test_queue_spike_exits_reduce_depth(db):
    """
    6 joined, but 4 of them exited → effective queue_depth = 2 → no spike anomaly.
    """
    for i in range(6):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_QE{i}", zone_id="BILLING")
    for i in range(4):
        await _ev(db, "EXIT", f"VIS_QE{i}")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "BILLING_QUEUE_SPIKE" not in types


@pytest.mark.asyncio
async def test_queue_spike_abandons_reduce_depth(db):
    """
    4 joined, 2 abandoned → queue_depth = 2 → below WARN threshold → no spike.
    """
    for i in range(4):
        await _ev(db, "BILLING_QUEUE_JOIN",    f"VIS_QA{i}", zone_id="BILLING")
    for i in range(2):
        await _ev(db, "BILLING_QUEUE_ABANDON", f"VIS_QA{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "BILLING_QUEUE_SPIKE" not in types


# ── Conversion drop tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversion_drop_not_flagged_below_visitor_threshold(db):
    """
    Only 5 visitors (total_visitors <= 10) → CONVERSION_DROP must NOT fire,
    even with 0% conversion.
    """
    for i in range(5):
        await _ev(db, "ENTRY", f"VIS_FEW{i}")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "CONVERSION_DROP" not in types


@pytest.mark.asyncio
async def test_conversion_drop_not_flagged_at_exactly_10(db):
    """Exactly 10 total_visitors: condition is total_visitors > 10 — NOT flagged at 10."""
    for i in range(10):
        await _ev(db, "ENTRY", f"VIS_TEN{i}")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "CONVERSION_DROP" not in types


@pytest.mark.asyncio
async def test_conversion_drop_flagged_with_zero_conversion(db):
    """
    20 visitors (> 10), 0% conversion → CONVERSION_DROP WARN.
    """
    for i in range(20):
        await _ev(db, "ENTRY", f"VIS_CD{i}")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "CONVERSION_DROP" in types
    drop = next(a for a in r.anomalies if a.anomaly_type == "CONVERSION_DROP")
    assert drop.severity == "WARN"


@pytest.mark.asyncio
async def test_conversion_drop_not_flagged_above_10_pct(db):
    """
    11 total visitors, 2 converted (18%) → above 10% → NO CONVERSION_DROP.
    """
    event_ts = _ts(0)
    pos_ts   = _ts(5)   # 5 min later, within 1800s
    for i in range(11):
        await _ev(db, "ENTRY", f"VIS_OK{i}", timestamp=event_ts)
    # 2 visitors enter BILLING zone and get POS
    await _ev(db, "ZONE_ENTER", "VIS_OK0", zone_id="BILLING", timestamp=event_ts)
    await _ev(db, "ZONE_ENTER", "VIS_OK1", zone_id="BILLING", timestamp=event_ts)
    await _pos(db, ts=pos_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "CONVERSION_DROP" not in types


# ── Dead zone tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dead_zone_detected(db):
    """
    CRITICAL FIX: Dead zone logic is REPLAY-RELATIVE, not wall-clock.

    Pattern:
      - Insert an old ZONE_ENTER for zone "DEAD_ZONE_X" (e.g. 60 min ago)
      - Insert a RECENT event (any type) to push MAX(timestamp) to ~now
      - With MAX(timestamp)≈now and cutoff=now-30min:
        - DEAD_ZONE_X's last visit (60 min ago) < cutoff → it's a dead zone ✓

    Without the recent event, MAX(timestamp) = old_ts, and cutoff = old_ts - 30min.
    The old ZONE_ENTER IS within [cutoff, MAX(timestamp)] so it appears active → no dead zone.
    """
    old_ts    = _ts(-60)    # 60 min ago (in replay time)
    recent_ts = _ts(0)      # now (pushes MAX forward)

    # Old zone visit
    await _ev(db, "ZONE_ENTER", "VIS_OLD", zone_id="DEAD_ZONE_X", timestamp=old_ts)
    # Recent event (not ZONE_ENTER, just pushes MAX timestamp) — different visitor
    await _ev(db, "ENTRY", "VIS_RECENT", timestamp=recent_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "DEAD_ZONE" in types
    dead_anom = [a for a in r.anomalies if a.anomaly_type == "DEAD_ZONE"]
    zone_ids  = [a.description for a in dead_anom]
    # At least one DEAD_ZONE anomaly for DEAD_ZONE_X
    assert any("DEAD_ZONE_X" in d for d in zone_ids)


@pytest.mark.asyncio
async def test_dead_zone_not_triggered_for_recent_visit(db):
    """
    Zone with a ZONE_ENTER within the last 30 min (relative to MAX(timestamp)) is NOT dead.
    Single event: MAX(timestamp) = that event's timestamp, cutoff = ts - 30min.
    The event IS within [cutoff, MAX] so zone is active → no dead zone.
    """
    recent_ts = _ts(0)
    await _ev(db, "ZONE_ENTER", "VIS_ACT", zone_id="ACTIVE_ZONE", timestamp=recent_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    types = [a.anomaly_type for a in r.anomalies]
    assert "DEAD_ZONE" not in types


@pytest.mark.asyncio
async def test_dead_zone_excludes_billing_zones(db):
    """
    BILLING zones must never be flagged as dead zones, regardless of last visit time.
    Only non-BILLING zones are checked for dead zone status.
    """
    old_ts    = _ts(-60)
    recent_ts = _ts(0)

    # Old visit to a BILLING zone — should NOT be flagged
    await _ev(db, "ZONE_ENTER", "VIS_BLD", zone_id="ST1076_Z_BILLING_01", timestamp=old_ts)
    await _ev(db, "ENTRY",      "VIS_NEW", timestamp=recent_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    for a in r.anomalies:
        if a.anomaly_type == "DEAD_ZONE":
            assert "BILLING" not in a.description


@pytest.mark.asyncio
async def test_dead_zone_only_customer_zones(db):
    """Staff ZONE_ENTER events are excluded from dead zone tracking."""
    old_ts    = _ts(-60)
    recent_ts = _ts(0)

    # Staff-only visit to a zone — since is_staff=1 is excluded, the zone has no customer visit
    # With no customer ZONE_ENTER, all_zones is empty → no dead zones
    await _ev(db, "ZONE_ENTER", "VIS_SFF", zone_id="STAFF_ZONE", is_staff=1, timestamp=old_ts)
    await _ev(db, "ENTRY",      "VIS_NEW",                                    timestamp=recent_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    # STAFF_ZONE never appears in customer ZONE_ENTER → not in all_zones → not flagged
    types = [a.anomaly_type for a in r.anomalies]
    assert "DEAD_ZONE" not in types


@pytest.mark.asyncio
async def test_dead_zone_multiple_zones(db):
    """Multiple dead zones → multiple DEAD_ZONE anomalies, one per zone."""
    old_ts    = _ts(-60)
    recent_ts = _ts(0)

    await _ev(db, "ZONE_ENTER", "VIS_D1", zone_id="ZONE_ALPHA", timestamp=old_ts)
    await _ev(db, "ZONE_ENTER", "VIS_D2", zone_id="ZONE_BETA",  timestamp=old_ts)
    await _ev(db, "ENTRY",      "VIS_NEW",                       timestamp=recent_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    dead = [a for a in r.anomalies if a.anomaly_type == "DEAD_ZONE"]
    assert len(dead) == 2
    zones_described = " ".join(a.description for a in dead)
    assert "ZONE_ALPHA" in zones_described
    assert "ZONE_BETA"  in zones_described


# ── Anomaly structure tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anomaly_fields_present(db):
    """Each anomaly must have all required fields and valid values."""
    for i in range(4):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_FLD{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    assert len(r.anomalies) > 0
    for a in r.anomalies:
        assert a.anomaly_type
        assert a.severity in ("INFO", "WARN", "CRITICAL")
        assert a.description
        assert a.suggested_action
        assert a.detected_at


@pytest.mark.asyncio
async def test_anomaly_severity_values_valid(db):
    """All severity values must be one of INFO, WARN, CRITICAL."""
    for i in range(4):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_SV{i}", zone_id="BILLING")
    await db.commit()

    r = await compute_anomalies(STORE, db)
    valid = {"INFO", "WARN", "CRITICAL"}
    for a in r.anomalies:
        assert a.severity in valid


@pytest.mark.asyncio
async def test_dead_zone_severity_is_info(db):
    """DEAD_ZONE anomalies must have severity=INFO."""
    old_ts    = _ts(-60)
    recent_ts = _ts(0)
    await _ev(db, "ZONE_ENTER", "VIS_OLD", zone_id="SLEEPY_ZONE", timestamp=old_ts)
    await _ev(db, "ENTRY",      "VIS_NOW",                         timestamp=recent_ts)
    await db.commit()

    r = await compute_anomalies(STORE, db)
    for a in r.anomalies:
        if a.anomaly_type == "DEAD_ZONE":
            assert a.severity == "INFO"


# ── HTTP endpoint smoke tests (via conftest client) ────────────────────────────

@pytest.mark.asyncio
async def test_anomalies_endpoint_empty_store(client):
    """Empty store → no anomalies (anomalies == [])."""
    resp = await client.get("/stores/ST1076/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert "anomalies" in body
    assert "store_id" in body
    assert "computed_at" in body
    assert body["anomalies"] == []


@pytest.mark.asyncio
async def test_anomalies_endpoint_queue_spike(client, db_session):
    """Ingest 7 queue joins → CRITICAL BILLING_QUEUE_SPIKE via HTTP."""
    for i in range(7):
        await db_session.execute(text("""
            INSERT OR IGNORE INTO events
              (event_id, store_id, camera_id, visitor_id, event_type,
               timestamp, zone_id, dwell_ms, is_staff, confidence,
               queue_depth, sku_zone, session_seq, ingested_at)
            VALUES
              (:eid, 'ST1076', 'CAM3', :vid, 'BILLING_QUEUE_JOIN',
               '2026-04-10T10:00:00Z', 'ST1076_Z_BILLING_01', 0, 0, 0.9,
               NULL, NULL, 0, '2026-04-10T10:00:00Z')
        """), {"eid": str(uuid.uuid4()), "vid": f"VIS_QSP{i}"})
    await db_session.commit()

    resp = await client.get("/stores/ST1076/anomalies")
    body = resp.json()
    types = [a["anomaly_type"] for a in body["anomalies"]]
    assert "BILLING_QUEUE_SPIKE" in types
    spike = next(a for a in body["anomalies"] if a["anomaly_type"] == "BILLING_QUEUE_SPIKE")
    assert spike["severity"] == "CRITICAL"
    assert spike["suggested_action"]


@pytest.mark.asyncio
async def test_anomalies_endpoint_conversion_drop(client, db_session):
    """20 ENTRY visitors, no purchases → CONVERSION_DROP via HTTP."""
    for i in range(20):
        await db_session.execute(text("""
            INSERT OR IGNORE INTO events
              (event_id, store_id, camera_id, visitor_id, event_type,
               timestamp, zone_id, dwell_ms, is_staff, confidence,
               queue_depth, sku_zone, session_seq, ingested_at)
            VALUES
              (:eid, 'ST1076', 'CAM3', :vid, 'ENTRY',
               '2026-04-10T10:00:00Z', NULL, 0, 0, 0.9,
               NULL, NULL, 0, '2026-04-10T10:00:00Z')
        """), {"eid": str(uuid.uuid4()), "vid": f"VIS_CVDR{i}"})
    await db_session.commit()

    resp = await client.get("/stores/ST1076/anomalies")
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "CONVERSION_DROP" in types