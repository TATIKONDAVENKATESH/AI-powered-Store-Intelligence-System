"""
test_metrics.py — Tests for GET /stores/{store_id}/metrics

What this tests:
  - Zero-visitor store returns 0s (not null, not crash)
  - unique_visitors counts distinct visitor_ids with is_staff=0
  - Staff events are excluded from unique_visitors
  - queue_depth = visitors who did BILLING_QUEUE_JOIN minus those who exited or abandoned
  - abandonment_rate = abandoned / joined (visitors, not events)
  - conversion_rate = converted_visitors / unique_visitors (BILLING zone + POS correlation)
  - avg_dwell_per_zone computed from ZONE_DWELL and ZONE_ENTER events
  - total_transactions = COUNT of pos_transactions for the store
  - Response schema contains all required fields

Key production behaviour:
  - Conversion requires: visitor in BILLING zone AND a POS transaction within 1800s AFTER that event
  - queue_depth excludes visitors who later EXIT or BILLING_QUEUE_ABANDON
  - All visitor-counting queries use is_staff = 0
"""
from __future__ import annotations

import uuid
import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

from unittest.mock import AsyncMock, Mock
from app.metrics import compute_metrics



# ── Helpers ──────────────────────────────────────────────────────────────────

def _ev(
    store_id: str,
    event_type: str,
    visitor_id: str,
    zone_id: str = None,
    is_staff: int = 0,
    dwell_ms: int = 0,
    timestamp: str = None,
    camera_id: str = "CAM_TEST",
    sku_zone: str = None,
) -> dict:
    """Build a complete event row dict for direct DB insertion."""
    return {
        "event_id":    str(uuid.uuid4()),
        "store_id":    store_id,
        "camera_id":   camera_id,
        "visitor_id":  visitor_id,
        "event_type":  event_type,
        "timestamp":   timestamp or "2026-04-10T07:00:00+00:00",
        "zone_id":     zone_id,
        "dwell_ms":    dwell_ms,
        "is_staff":    is_staff,
        "confidence":  0.9,
        "queue_depth": None,
        "sku_zone":    sku_zone,
        "session_seq": 0,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


async def _insert(db, rows: list[dict]) -> None:
    for row in rows:
        await db.execute(text("""
            INSERT OR IGNORE INTO events
              (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
               zone_id, dwell_ms, is_staff, confidence, queue_depth, sku_zone,
               session_seq, ingested_at)
            VALUES
              (:event_id, :store_id, :camera_id, :visitor_id, :event_type, :timestamp,
               :zone_id, :dwell_ms, :is_staff, :confidence, :queue_depth, :sku_zone,
               :session_seq, :ingested_at)
        """), row)
    await db.commit()


async def _insert_pos(db, store_id: str, timestamp: str, basket_value: float = 500.0) -> None:
    await db.execute(text("""
        INSERT OR IGNORE INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
        VALUES (:tid, :sid, :ts, :bv)
    """), {
        "tid": str(uuid.uuid4()),
        "sid": store_id,
        "ts":  timestamp,
        "bv":  basket_value,
    })
    await db.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_zero_visitors(client):
    """Empty store must return all zeros — not null, not 422, not 503."""
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["queue_depth"] == 0
    assert body["abandonment_rate"] == 0.0
    assert body["total_transactions"] == 0
    assert body["avg_dwell_per_zone"] == []


@pytest.mark.asyncio
async def test_metrics_response_schema(client):
    """All fields required by MetricsResponse model must be present."""
    resp = await client.get("/stores/ST1076/metrics")
    body = resp.json()
    for field in [
        "store_id", "unique_visitors", "conversion_rate",
        "avg_dwell_per_zone", "queue_depth", "abandonment_rate",
        "total_transactions", "computed_at",
    ]:
        assert field in body, f"Missing field: {field}"
    assert body["store_id"] == "ST1076"


@pytest.mark.asyncio
async def test_metrics_unique_visitors_count(client, db_session):
    """Three distinct visitor_ids → unique_visitors == 3."""
    rows = [
        _ev("ST1076", "ENTRY", "VIS_M001"),
        _ev("ST1076", "ENTRY", "VIS_M002"),
        _ev("ST1076", "ENTRY", "VIS_M003"),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["unique_visitors"] == 3


@pytest.mark.asyncio
async def test_metrics_deduplication_same_visitor(client, db_session):
    """Same visitor_id appearing multiple times counts as 1 unique visitor."""
    rows = [
        _ev("ST1076", "ENTRY",      "VIS_DUP"),
        _ev("ST1076", "ZONE_ENTER", "VIS_DUP", zone_id="Z01"),
        _ev("ST1076", "EXIT",       "VIS_DUP"),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["unique_visitors"] == 1


@pytest.mark.asyncio
async def test_metrics_staff_excluded_from_unique_visitors(client, db_session):
    """Staff events (is_staff=1) must NOT appear in unique_visitors count."""
    rows = [
        _ev("ST1076", "ENTRY", "VIS_CUST_A",  is_staff=0),
        _ev("ST1076", "ENTRY", "VIS_CUST_B",  is_staff=0),
        _ev("ST1076", "ENTRY", "VIS_STAFF_1", is_staff=1),
        _ev("ST1076", "ENTRY", "VIS_STAFF_2", is_staff=1),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    # Only the 2 customer visitors should be counted
    assert resp.json()["unique_visitors"] == 2


@pytest.mark.asyncio
async def test_metrics_queue_depth_join_minus_exit(client, db_session):
    """
    queue_depth = visitors who joined and have NOT yet exited or abandoned.
    VIS_Q1 joined + exited → NOT in queue.
    VIS_Q2 joined + abandoned → NOT in queue.
    VIS_Q3 joined only → IN queue.
    Expected queue_depth = 1.
    """
    rows = [
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_Q1", zone_id="ST1076_Z_BILLING_01"),
        _ev("ST1076", "EXIT",                  "VIS_Q1"),
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_Q2", zone_id="ST1076_Z_BILLING_01"),
        _ev("ST1076", "BILLING_QUEUE_ABANDON", "VIS_Q2", zone_id="ST1076_Z_BILLING_01"),
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_Q3", zone_id="ST1076_Z_BILLING_01"),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["queue_depth"] == 1


@pytest.mark.asyncio
async def test_metrics_queue_depth_multiple_in_queue(client, db_session):
    """Multiple visitors who joined and have not left → queue_depth counts all."""
    rows = [
        _ev("ST1076", "BILLING_QUEUE_JOIN", f"VIS_QQ{i}", zone_id="ST1076_Z_BILLING_01")
        for i in range(5)
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["queue_depth"] == 5


@pytest.mark.asyncio
async def test_metrics_abandonment_rate(client, db_session):
    """
    4 joined, 2 abandoned → abandonment_rate = 2/4 = 0.5.
    """
    rows = [
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_A1", zone_id="BILLING"),
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_A2", zone_id="BILLING"),
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_A3", zone_id="BILLING"),
        _ev("ST1076", "BILLING_QUEUE_JOIN",    "VIS_A4", zone_id="BILLING"),
        _ev("ST1076", "BILLING_QUEUE_ABANDON", "VIS_A1", zone_id="BILLING"),
        _ev("ST1076", "BILLING_QUEUE_ABANDON", "VIS_A2", zone_id="BILLING"),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    rate = resp.json()["abandonment_rate"]
    assert abs(rate - 0.5) < 0.001


@pytest.mark.asyncio
async def test_metrics_conversion_rate_with_pos(client, db_session):
    """
    Conversion: visitor with BILLING zone event + POS transaction within 1800s.
    3 visitors, 1 in BILLING zone + POS match → conversion_rate = 1/3.
    """
    event_ts = "2026-04-10T10:00:00+00:00"
    pos_ts   = "2026-04-10T10:05:00+00:00"   # 5 min after → within 1800s

    rows = [
        _ev("ST1076", "ENTRY",      "VIS_C1", timestamp=event_ts),
        _ev("ST1076", "ENTRY",      "VIS_C2", timestamp=event_ts),
        _ev("ST1076", "ENTRY",      "VIS_C3", timestamp=event_ts),
        # Only VIS_C1 enters BILLING zone
        _ev("ST1076", "ZONE_ENTER", "VIS_C1", zone_id="ST1076_Z_BILLING_01", timestamp=event_ts),
    ]
    await _insert(db_session, rows)
    await _insert_pos(db_session, "ST1076", pos_ts)

    resp = await client.get("/stores/ST1076/metrics")
    body = resp.json()
    rate = body["conversion_rate"]
    # 1 converted out of 3 visitors
    assert abs(rate - round(1/3, 4)) < 0.001


@pytest.mark.asyncio
async def test_metrics_no_conversion_without_pos(client, db_session):
    """Visitors in BILLING zone but NO POS transaction → conversion_rate = 0."""
    rows = [
        _ev("ST1076", "ZONE_ENTER", "VIS_NC1", zone_id="BILLING"),
        _ev("ST1076", "ZONE_ENTER", "VIS_NC2", zone_id="BILLING"),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["conversion_rate"] == 0.0


@pytest.mark.asyncio
async def test_metrics_total_transactions(client, db_session):
    """total_transactions reflects the count of pos_transactions for the store."""
    await _insert_pos(db_session, "ST1076", "2026-04-10T10:00:00+00:00", 300.0)
    await _insert_pos(db_session, "ST1076", "2026-04-10T11:00:00+00:00", 450.0)
    # Different store — should NOT count
    await _insert_pos(db_session, "ST1008", "2026-04-10T10:30:00+00:00", 200.0)

    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["total_transactions"] == 2


@pytest.mark.asyncio
async def test_metrics_avg_dwell_per_zone(client, db_session):
    """avg_dwell_per_zone is populated from ZONE_DWELL and ZONE_ENTER events."""
    rows = [
        _ev("ST1076", "ZONE_DWELL", "VIS_D1", zone_id="ZONE_A", dwell_ms=30000),
        _ev("ST1076", "ZONE_DWELL", "VIS_D2", zone_id="ZONE_A", dwell_ms=10000),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    body = resp.json()
    zones = {z["zone_id"]: z for z in body["avg_dwell_per_zone"]}
    assert "ZONE_A" in zones
    # avg dwell = (30000 + 10000) / 2 / 1000 = 20.0 seconds
    assert abs(zones["ZONE_A"]["avg_dwell_seconds"] - 20.0) < 0.1
    assert zones["ZONE_A"]["visit_count"] == 2


@pytest.mark.asyncio
async def test_metrics_store_isolation(client, db_session):
    """Events for ST1008 must not appear in ST1076 metrics and vice versa."""
    rows = [
        _ev("ST1008", "ENTRY", "VIS_OTHER_1"),
        _ev("ST1008", "ENTRY", "VIS_OTHER_2"),
    ]
    await _insert(db_session, rows)
    resp = await client.get("/stores/ST1076/metrics")
    assert resp.json()["unique_visitors"] == 0


@pytest.mark.asyncio
async def test_metrics_unknown_store_returns_zeros(client):
    """Unknown store ID must return 200 with all zeros — not 404."""
    resp = await client.get("/stores/STORE_DOES_NOT_EXIST/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0

class FakeResult:
    def __init__(self, scalar_value=None, rows=None, row=None):
        self._scalar = scalar_value
        self._rows = rows or []
        self._row = row

    def scalar(self):
        return self._scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


@pytest.mark.asyncio
async def test_compute_metrics_all_zero():
    db = AsyncMock()

    db.execute.side_effect = [
        FakeResult(scalar_value=0),      # visitors
        FakeResult(scalar_value=0),      # transactions
        FakeResult(scalar_value=0),      # converted
        FakeResult(rows=[]),             # dwell
        FakeResult(scalar_value=0),      # queue
        FakeResult(row=(0, 0)),          # abandon
    ]

    result = await compute_metrics("STORE1", db)

    assert result.unique_visitors == 0
    assert result.conversion_rate == 0.0
    assert result.queue_depth == 0
    assert result.abandonment_rate == 0.0
    assert result.total_transactions == 0
    assert result.avg_dwell_per_zone == []


@pytest.mark.asyncio
async def test_compute_metrics_full_values():
    db = AsyncMock()

    db.execute.side_effect = [
        FakeResult(scalar_value=10),      # visitors
        FakeResult(scalar_value=5),       # transactions
        FakeResult(scalar_value=4),       # converted
        FakeResult(
            rows=[
                ("ZONE_A", 12.345, 7),
                ("ZONE_B", 5.5, 2),
            ]
        ),
        FakeResult(scalar_value=3),       # queue
        FakeResult(row=(8, 2)),           # joined, abandoned
    ]

    result = await compute_metrics("STORE1", db)

    assert result.unique_visitors == 10
    assert result.total_transactions == 5
    assert result.queue_depth == 3

    assert result.conversion_rate == 0.4
    assert result.abandonment_rate == 0.25

    assert len(result.avg_dwell_per_zone) == 2

    assert result.avg_dwell_per_zone[0].zone_id == "ZONE_A"
    assert result.avg_dwell_per_zone[0].visit_count == 7

    assert result.avg_dwell_per_zone[1].zone_id == "ZONE_B"


@pytest.mark.asyncio
async def test_compute_metrics_joined_without_abandonment():
    db = AsyncMock()

    db.execute.side_effect = [
        FakeResult(scalar_value=2),      # visitors
        FakeResult(scalar_value=1),      # transactions
        FakeResult(scalar_value=1),      # converted
        FakeResult(rows=[]),
        FakeResult(scalar_value=1),
        FakeResult(row=(5, 0)),          # joined, abandoned
    ]

    result = await compute_metrics("STORE1", db)

    assert result.abandonment_rate == 0.0


@pytest.mark.asyncio
async def test_compute_metrics_zone_dwell_rounding():
    db = AsyncMock()

    db.execute.side_effect = [
        FakeResult(scalar_value=1),
        FakeResult(scalar_value=0),
        FakeResult(scalar_value=0),
        FakeResult(
            rows=[
                ("ZONE_X", 1.2367, 3),
            ]
        ),
        FakeResult(scalar_value=0),
        FakeResult(row=(0, 0)),
    ]

    result = await compute_metrics("STORE1", db)

    assert result.avg_dwell_per_zone[0].avg_dwell_seconds == 1.24