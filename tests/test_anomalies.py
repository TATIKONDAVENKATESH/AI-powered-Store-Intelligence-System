# PROMPT: "Write async pytest tests for an anomaly detection endpoint. Cover: no anomalies
# on empty store, queue spike WARN at depth=3, CRITICAL at depth=6, conversion drop WARN
# when rate < 10% with enough traffic, dead zone INFO when a zone has no visits in 30 min."
# CHANGES MADE: Used BILLING zone_id matching LIKE '%BILLING%'; added test for empty
# anomaly list; aligned queue thresholds with anomalies.py constants.

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import text


def _ev(store_id, event_type, visitor_id, zone_id=None, is_staff=0,
        timestamp=None, camera_id="CAM3"):
    ts = timestamp or "2026-04-10T07:00:00+00:00"
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  ts,
        "zone_id":    zone_id,
        "dwell_ms":   0,
        "is_staff":   is_staff,
        "confidence": 0.9,
        "queue_depth": None,
        "sku_zone":   None,
        "session_seq": 1,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


async def _insert(db, rows):
    for row in rows:
        await db.execute(text("""
            INSERT OR IGNORE INTO events
              (event_id,store_id,camera_id,visitor_id,event_type,timestamp,
               zone_id,dwell_ms,is_staff,confidence,queue_depth,sku_zone,session_seq,ingested_at)
            VALUES
              (:event_id,:store_id,:camera_id,:visitor_id,:event_type,:timestamp,
               :zone_id,:dwell_ms,:is_staff,:confidence,:queue_depth,:sku_zone,:session_seq,:ingested_at)
        """), row)
    await db.commit()


@pytest.mark.asyncio
async def test_no_anomalies_empty_store(client):
    resp = await client.get("/stores/ST1076/anomalies")
    assert resp.status_code == 200
    assert resp.json()["anomalies"] == []


@pytest.mark.asyncio
async def test_queue_spike_warn(client, db_session):
    rows = [
        _ev("ST1076", "BILLING_QUEUE_JOIN", f"VIS_Q{i}", zone_id="ST1076_Z_BILLING_01")
        for i in range(4)  # 4 visitors in queue → WARN (threshold=3)
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/anomalies")
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "BILLING_QUEUE_SPIKE" in types
    severities = {a["anomaly_type"]: a["severity"] for a in resp.json()["anomalies"]}
    assert severities["BILLING_QUEUE_SPIKE"] in ("WARN", "CRITICAL")


@pytest.mark.asyncio
async def test_queue_spike_critical(client, db_session):
    rows = [
        _ev("ST1076", "BILLING_QUEUE_JOIN", f"VIS_Q{i}", zone_id="ST1076_Z_BILLING_01")
        for i in range(7)  # 7 visitors → CRITICAL (threshold=6)
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/anomalies")
    severities = {a["anomaly_type"]: a["severity"] for a in resp.json()["anomalies"]}
    assert severities.get("BILLING_QUEUE_SPIKE") == "CRITICAL"


@pytest.mark.asyncio
async def test_conversion_drop_warn(client, db_session):
    # 20 visitors entered, 0 converted → rate = 0% < 10%
    rows = [
        _ev("ST1076", "ENTRY", f"VIS_{i}")
        for i in range(20)
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/anomalies")
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "CONVERSION_DROP" in types


@pytest.mark.asyncio
async def test_dead_zone_detected(client, db_session):
    # Zone with an old visit (>30 min ago) — should trigger DEAD_ZONE
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    rows = [
        _ev("ST1076", "ZONE_ENTER", "VIS_OLD", zone_id="ST1076_Z01", timestamp=old_ts),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/anomalies")
    types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
    assert "DEAD_ZONE" in types


@pytest.mark.asyncio
async def test_anomaly_has_suggested_action(client, db_session):
    rows = [
        _ev("ST1076", "BILLING_QUEUE_JOIN", f"VIS_Q{i}", zone_id="ST1076_Z_BILLING_01")
        for i in range(4)
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/anomalies")
    for anomaly in resp.json()["anomalies"]:
        assert anomaly["suggested_action"]  # must not be empty
        assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")