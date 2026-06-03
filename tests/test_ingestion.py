"""
test_ingestion.py — Tests for app/ingestion.py

Covers:
  - ingest_events(): happy path, idempotency, empty batch, staff flag, error path
  - load_pos_transactions(): CSV loading, idempotency, multi-SKU aggregation,
    bad amount, bad date, missing identifying fields
  - build_ingest_batches(): splitting logic

IMPORTANT FIXES vs original test_ingest.py:

1. test_load_pos_missing_invoice_number_skipped is WRONG.
   When invoice_number is empty/absent, load_pos_transactions falls back to
   composite key: "{store_id}_{order_date}_{order_time}".
   A row with store_id="ST1008", valid date/time WILL be loaded (not skipped).
   The fixed test verifies it IS loaded via the composite key path.

2. Real POS CSV (data/pos_transactions.csv) has columns:
   order_id, order_date, order_time, store_id, product_id, brand_name, total_amount
   — NO invoice_number column.
   The _write_pos_csv helper correctly omits invoice_number to test the composite key path.

3. Rows are only skipped when composite key cannot be formed:
   missing store_id AND missing date AND missing time simultaneously.
"""
from __future__ import annotations

import os
import sys
import csv
import uuid
import pytest
import pytest_asyncio
from unittest.mock import MagicMock
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import StoreEvent, EventMetadata
from app.ingestion import ingest_events, load_pos_transactions, build_ingest_batches

_SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
)


@pytest_asyncio.fixture
async def db_session():
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


# ── Event builder ─────────────────────────────────────────────────────────────

def _event(event_id: str = None, store_id: str = "ST1076") -> StoreEvent:
    return StoreEvent(
        event_id=event_id or str(uuid.uuid4()),
        store_id=store_id,
        camera_id="CAM_ENTRY_01",
        visitor_id="VIS_TEST_0001",
        event_type="ENTRY",
        timestamp="2026-04-10T10:00:00+00:00",
        confidence=0.9,
        metadata=EventMetadata(),
    )


# ── POS CSV helpers ────────────────────────────────────────────────────────────
# Matches the ACTUAL POS CSV format: order_id, order_date, order_time, store_id,
# product_id, brand_name, total_amount  (no invoice_number in real CSV)

def _write_pos_csv_minimal(path: str, rows: list[dict]) -> None:
    """Write POS CSV with only the columns the real CSV has (no invoice_number)."""
    fieldnames = ["order_id", "order_date", "order_time", "store_id",
                  "product_id", "brand_name", "total_amount"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            full = {k: "" for k in fieldnames}
            full.update(row)
            writer.writerow(full)


def _write_pos_csv_with_invoice(path: str, rows: list[dict]) -> None:
    """Write POS CSV that includes an invoice_number column (enriched test format)."""
    fieldnames = [
        "order_id", "order_date", "order_time", "store_id",
        "product_id", "brand_name", "total_amount", "invoice_number",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            full = {k: "" for k in fieldnames}
            full.update(row)
            writer.writerow(full)


# ── ingest_events: happy path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_single_event(db_session):
    ev = _event()
    result = await ingest_events([ev], db_session)
    assert result.accepted == 1
    assert result.rejected == 0
    assert result.duplicates == 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_ingest_batch_multiple_events(db_session):
    events = [_event() for _ in range(10)]
    result = await ingest_events(events, db_session)
    assert result.accepted == 10
    assert result.rejected == 0
    assert result.duplicates == 0


@pytest.mark.asyncio
async def test_ingest_empty_batch(db_session):
    result = await ingest_events([], db_session)
    assert result.accepted == 0
    assert result.rejected == 0
    assert result.duplicates == 0


# ── ingest_events: idempotency ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_duplicate_counted_not_inserted(db_session):
    """Same event ingested twice: second call → duplicates=1, accepted=0."""
    ev = _event()
    r1 = await ingest_events([ev], db_session)
    r2 = await ingest_events([ev], db_session)
    assert r1.accepted == 1
    assert r2.accepted == 0
    assert r2.duplicates == 1


@pytest.mark.asyncio
async def test_ingest_same_batch_twice_all_duplicates(db_session):
    events = [_event() for _ in range(5)]
    r1 = await ingest_events(events, db_session)
    r2 = await ingest_events(events, db_session)
    assert r1.accepted == 5
    assert r2.accepted == 0
    assert r2.duplicates == 5


@pytest.mark.asyncio
async def test_ingest_mixed_new_and_duplicate(db_session):
    """First event is duplicate, second is new."""
    ev1 = _event()
    ev2 = _event()
    await ingest_events([ev1], db_session)
    result = await ingest_events([ev1, ev2], db_session)
    assert result.accepted == 1
    assert result.duplicates == 1


# ── ingest_events: data persistence ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_staff_flag_stored_as_integer(db_session):
    """is_staff=True must be stored as integer 1 in SQLite."""
    ev = StoreEvent(
        event_id=str(uuid.uuid4()),
        store_id="ST1076",
        camera_id="CAM_STAFF",
        visitor_id="VIS_STAFF_001",
        event_type="ENTRY",
        timestamp="2026-04-10T10:05:00+00:00",
        confidence=0.95,
        is_staff=True,
        metadata=EventMetadata(),
    )
    await ingest_events([ev], db_session)
    row = await db_session.execute(
        text("SELECT is_staff FROM events WHERE visitor_id='VIS_STAFF_001'")
    )
    assert row.scalar() == 1


@pytest.mark.asyncio
async def test_ingest_all_fields_persisted(db_session):
    """Verify all event fields are persisted correctly to the events table."""
    eid = str(uuid.uuid4())
    ev = StoreEvent(
        event_id=eid,
        store_id="ST1076",
        camera_id="CAM3",
        visitor_id="VIS_FULL",
        event_type="ZONE_DWELL",
        timestamp="2026-04-10T10:00:00+00:00",
        zone_id="SKINCARE_TOP",
        dwell_ms=15000,
        confidence=0.88,
        is_staff=False,
        metadata=EventMetadata(queue_depth=None, sku_zone="SKINCARE", session_seq=3),
    )
    await ingest_events([ev], db_session)
    row = await db_session.execute(
        text("SELECT store_id, camera_id, visitor_id, event_type, zone_id, dwell_ms, "
             "confidence, is_staff, sku_zone, session_seq FROM events WHERE event_id=:eid"),
        {"eid": eid}
    )
    r = row.fetchone()
    assert r[0] == "ST1076"
    assert r[1] == "CAM3"
    assert r[2] == "VIS_FULL"
    assert r[3] == "ZONE_DWELL"
    assert r[4] == "SKINCARE_TOP"
    assert r[5] == 15000
    assert abs(r[6] - 0.88) < 0.001
    assert r[7] == 0
    assert r[8] == "SKINCARE"
    assert r[9] == 3


# ── ingest_events: error handling ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_db_error_counted_as_rejected(db_session):
    """
    Exception during INSERT is caught → event counted as rejected, error logged.
    Strategy: make metadata.queue_depth raise on access inside the try block.
    """
    class BrokenMeta:
        @property
        def queue_depth(self):
            raise RuntimeError("Simulated DB error")
        sku_zone    = None
        session_seq = 0

    bad_ev = _event()
    bad_ev.metadata = BrokenMeta()

    result = await ingest_events([bad_ev], db_session)
    assert result.rejected == 1
    assert len(result.errors) == 1
    assert "Simulated DB error" in result.errors[0]


@pytest.mark.asyncio
async def test_ingest_error_does_not_prevent_other_events(db_session):
    """A rejected event must not block subsequent events in the same batch."""
    class BrokenMeta:
        @property
        def queue_depth(self):
            raise RuntimeError("fail")
        sku_zone    = None
        session_seq = 0

    bad_ev  = _event()
    bad_ev.metadata = BrokenMeta()
    good_ev = _event()

    result = await ingest_events([bad_ev, good_ev], db_session)
    assert result.accepted == 1
    assert result.rejected == 1


# ── load_pos_transactions: composite key (real CSV format) ────────────────────

@pytest.mark.asyncio
async def test_load_pos_composite_key_real_csv_format(db_session, tmp_path):
    """
    Real CSV has no invoice_number. Key = {store_id}_{order_date}_{order_time}.
    Row must be loaded successfully.
    """
    csv_file = tmp_path / "pos.csv"
    _write_pos_csv_minimal(str(csv_file), [{
        "order_id":    "1",
        "order_date":  "10-04-2026",
        "order_time":  "14:30:00",
        "store_id":    "ST1008",
        "total_amount": "500.00",
    }])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    assert loaded == 1


@pytest.mark.asyncio
async def test_load_pos_idempotent(db_session, tmp_path):
    """Loading the same POS CSV twice: second load returns 0 (all rows already exist)."""
    csv_file = tmp_path / "pos_idem.csv"
    _write_pos_csv_minimal(str(csv_file), [{
        "order_id":    "2",
        "order_date":  "10-04-2026",
        "order_time":  "15:00:00",
        "store_id":    "ST1008",
        "total_amount": "299.00",
    }])
    n1 = await load_pos_transactions(str(csv_file), db_session)
    n2 = await load_pos_transactions(str(csv_file), db_session)
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_load_pos_multi_sku_same_order_summed(db_session, tmp_path):
    """
    Two rows with same order_id + date + time → same composite key → summed into one transaction.
    Composite key = {store_id}_{order_date}_{order_time}.
    """
    csv_file = tmp_path / "pos_multi.csv"
    _write_pos_csv_minimal(str(csv_file), [
        {"order_id": "3", "order_date": "10-04-2026", "order_time": "16:00:00",
         "store_id": "ST1008", "total_amount": "400.00"},
        {"order_id": "3", "order_date": "10-04-2026", "order_time": "16:00:00",
         "store_id": "ST1008", "total_amount": "150.00"},
    ])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    assert loaded == 1
    key = "ST1008_10-04-2026_16:00:00"
    row = await db_session.execute(
        text("SELECT basket_value FROM pos_transactions WHERE transaction_id=:tid"),
        {"tid": key}
    )
    val = row.scalar()
    assert abs(val - 550.0) < 0.01


@pytest.mark.asyncio
async def test_load_pos_invoice_number_used_when_present(db_session, tmp_path):
    """
    When invoice_number column IS present and non-empty, it is used as the transaction_id.
    """
    csv_file = tmp_path / "pos_inv.csv"
    _write_pos_csv_with_invoice(str(csv_file), [{
        "order_id":       "104363838",
        "invoice_number": "ML0426KAP0001358",
        "order_date":     "10-04-2026",
        "order_time":     "14:30:00",
        "store_id":       "ST1008",
        "total_amount":   "500.00",
    }])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    assert loaded == 1
    # The transaction_id should be the invoice number, not the composite key
    row = await db_session.execute(
        text("SELECT transaction_id FROM pos_transactions WHERE transaction_id='ML0426KAP0001358'")
    )
    assert row.scalar() == "ML0426KAP0001358"


@pytest.mark.asyncio
async def test_load_pos_empty_invoice_falls_back_to_composite(db_session, tmp_path):
    """
    Empty invoice_number → falls back to composite key and IS loaded.
    (Not skipped — the composite key is valid.)
    """
    csv_file = tmp_path / "pos_noinv.csv"
    _write_pos_csv_with_invoice(str(csv_file), [{
        "order_id":       "104363838",
        "invoice_number": "",            # empty → use composite key
        "order_date":     "10-04-2026",
        "order_time":     "13:00:00",
        "store_id":       "ST1008",
        "total_amount":   "350.00",
    }])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    # Composite key ST1008_10-04-2026_13:00:00 is valid → row loaded
    assert loaded == 1


@pytest.mark.asyncio
async def test_load_pos_bad_total_amount_treated_as_zero(db_session, tmp_path):
    """
    Non-numeric total_amount: ValueError is caught, amount stays 0, row is still loaded.
    """
    csv_file = tmp_path / "pos_bad_amt.csv"
    _write_pos_csv_with_invoice(str(csv_file), [{
        "order_id":       "999",
        "invoice_number": "INV_BAD_AMT",
        "order_date":     "10-04-2026",
        "order_time":     "11:00:00",
        "store_id":       "ST1008",
        "total_amount":   "NOT_A_NUMBER",
    }])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    assert loaded == 1
    row = await db_session.execute(
        text("SELECT basket_value FROM pos_transactions WHERE transaction_id='INV_BAD_AMT'")
    )
    assert row.scalar() == 0.0


@pytest.mark.asyncio
async def test_load_pos_unparseable_date_row_skipped(db_session, tmp_path):
    """Row with completely unparseable date/time is skipped; valid rows still load."""
    csv_file = tmp_path / "pos_bad_date.csv"
    _write_pos_csv_with_invoice(str(csv_file), [
        {"order_id": "100", "invoice_number": "INV_BAD_DATE",
         "order_date": "BADDATE", "order_time": "BADTIME",
         "store_id": "ST1008", "total_amount": "100.00"},
        {"order_id": "101", "invoice_number": "INV_GOOD",
         "order_date": "10-04-2026", "order_time": "12:00:00",
         "store_id": "ST1008", "total_amount": "200.00"},
    ])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    assert loaded == 1   # only the valid row


@pytest.mark.asyncio
async def test_load_pos_missing_file_returns_zero(db_session, tmp_path):
    """FileNotFoundError is caught gracefully, returns 0."""
    result = await load_pos_transactions(str(tmp_path / "nonexistent.csv"), db_session)
    assert result == 0


@pytest.mark.asyncio
async def test_load_pos_row_with_no_identifying_info_skipped(db_session, tmp_path):
    """
    Row where store_id, order_date, order_time are all empty AND no invoice_number
    → cannot form any key → row skipped.
    """
    csv_file = tmp_path / "pos_empty.csv"
    _write_pos_csv_minimal(str(csv_file), [{
        "order_id":    "",
        "order_date":  "",
        "order_time":  "",
        "store_id":    "",
        "total_amount": "100.00",
    }])
    loaded = await load_pos_transactions(str(csv_file), db_session)
    assert loaded == 0


@pytest.mark.asyncio
async def test_load_pos_ist_to_utc_conversion(db_session, tmp_path):
    """
    Timestamps are in IST (UTC+5:30). A time of 12:00:00 IST should be stored as 06:30:00 UTC.
    """
    csv_file = tmp_path / "pos_ist.csv"
    _write_pos_csv_minimal(str(csv_file), [{
        "order_id":    "200",
        "order_date":  "10-04-2026",
        "order_time":  "12:00:00",
        "store_id":    "ST1008",
        "total_amount": "100.00",
    }])
    await load_pos_transactions(str(csv_file), db_session)
    row = await db_session.execute(
        text("SELECT timestamp FROM pos_transactions WHERE store_id='ST1008'")
    )
    ts_utc = row.scalar()
    assert ts_utc is not None
    # 12:00 IST = 06:30 UTC
    assert "06:30" in ts_utc


# ── build_ingest_batches ───────────────────────────────────────────────────────

def test_build_ingest_batches_splits_correctly():
    events = [_event() for _ in range(12)]
    batches = build_ingest_batches(events, batch_size=5)
    assert len(batches) == 3
    assert len(batches[0]) == 5
    assert len(batches[1]) == 5
    assert len(batches[2]) == 2


def test_build_ingest_batches_empty_input():
    assert build_ingest_batches([], batch_size=500) == []


def test_build_ingest_batches_single_batch():
    events = [_event() for _ in range(3)]
    batches = build_ingest_batches(events, batch_size=500)
    assert len(batches) == 1
    assert len(batches[0]) == 3


def test_build_ingest_batches_exact_multiple():
    events = [_event() for _ in range(10)]
    batches = build_ingest_batches(events, batch_size=5)
    assert len(batches) == 2
    assert all(len(b) == 5 for b in batches)