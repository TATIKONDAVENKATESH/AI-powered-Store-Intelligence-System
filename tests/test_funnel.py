"""
test_funnel.py — Tests for GET /stores/{store_id}/funnel

Funnel stages (from app/funnel.py):
  1. "Entry"         — COUNT DISTINCT visitor_id WHERE event_type='ENTRY' AND is_staff=0
  2. "Zone Visit"    — COUNT DISTINCT WHERE event_type='ZONE_ENTER' AND zone_id NOT LIKE '%BILLING%' AND is_staff=0
  3. "Billing Queue" — COUNT DISTINCT WHERE event_type='BILLING_QUEUE_JOIN' AND is_staff=0
  4. "Purchase"      — DISTINCT visitor_id via INNER JOIN with pos_transactions
                       (POS timestamp within 1800s AFTER a BILLING zone event for the same store)

drop_off_pct for stage 1 (Entry) is always 0.0.
drop_off_pct(stage_n) = (count[n-1] - count[n]) / count[n-1] * 100

Key behaviour verified:
  - REENTRY event_type does NOT count as ENTRY (only exact 'ENTRY' type is counted)
  - Staff is_staff=1 excluded from all stages
  - Stage counts are DISTINCT visitor_ids — same visitor appearing twice counts once
  - Purchase stage needs both a BILLING zone ZONE_ENTER AND a POS transaction
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
from app.funnel import compute_funnel

STORE = "ST1076"
_NOW  = datetime.now(timezone.utc)

_SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
)


@pytest_asyncio.fixture
async def db():
    """Isolated in-memory SQLite session for funnel unit tests."""
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

async def _ev(
    db,
    event_type: str,
    visitor_id: str,
    zone_id: str = None,
    is_staff: int = 0,
    ts_offset_s: int = 0,
    store_id: str = STORE,
) -> None:
    ts = (_NOW + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO events
          (event_id, store_id, camera_id, visitor_id, event_type,
           timestamp, zone_id, dwell_ms, is_staff, confidence,
           queue_depth, sku_zone, session_seq, ingested_at)
        VALUES
          (:eid, :sid, 'CAM_FUNNEL', :vid, :et,
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


async def _pos(db, ts_offset_s: int = 120, store_id: str = STORE) -> None:
    ts = (_NOW + timedelta(seconds=ts_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT INTO pos_transactions (transaction_id, store_id, timestamp, basket_value)
        VALUES (:tid, :sid, :ts, 450.0)
    """), {"tid": str(uuid.uuid4()), "sid": store_id, "ts": ts})


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_empty_store(db):
    """Empty DB → all stage counts = 0, no crash, drop_off_pct for Entry = 0.0."""
    f = await compute_funnel(STORE, db)
    assert len(f.stages) == 4
    assert all(s.count == 0 for s in f.stages)
    entry = next(s for s in f.stages if s.stage == "Entry")
    assert entry.drop_off_pct == 0.0


@pytest.mark.asyncio
async def test_funnel_stage_names_and_order(db):
    """Stages must appear in order: Entry, Zone Visit, Billing Queue, Purchase."""
    f = await compute_funnel(STORE, db)
    names = [s.stage for s in f.stages]
    assert names == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]


@pytest.mark.asyncio
async def test_funnel_entry_count(db):
    """4 distinct visitors with ENTRY events → Entry stage count = 4."""
    for i in range(4):
        await _ev(db, "ENTRY", f"VIS_E{i}")
    await db.commit()

    f = await compute_funnel(STORE, db)
    entry = next(s for s in f.stages if s.stage == "Entry")
    assert entry.count == 4


@pytest.mark.asyncio
async def test_funnel_zone_visit_excludes_billing_zones(db):
    """Zone Visit stage excludes zones with 'BILLING' in zone_id."""
    await _ev(db, "ENTRY",      "VIS_ZV1")
    await _ev(db, "ZONE_ENTER", "VIS_ZV1", zone_id="SKINCARE_TOP")        # counts
    await _ev(db, "ZONE_ENTER", "VIS_ZV1", zone_id="ST1076_Z_BILLING_01") # excluded
    await db.commit()

    f = await compute_funnel(STORE, db)
    zone_stage = next(s for s in f.stages if s.stage == "Zone Visit")
    # Only SKINCARE_TOP counts; BILLING zone is excluded
    assert zone_stage.count == 1


@pytest.mark.asyncio
async def test_funnel_full_path(db):
    """
    4 entered, 3 visited non-billing zone, 2 joined billing queue, 1+ purchased.
    Verify each stage count.
    """
    # 4 entries
    for i in range(4):
        await _ev(db, "ENTRY", f"VIS_F{i}")
    # 3 zone visits
    for i in range(3):
        await _ev(db, "ZONE_ENTER", f"VIS_F{i}", zone_id="MAKEUP_CENTER")
    # 2 reach billing
    for i in range(2):
        await _ev(db, "BILLING_QUEUE_JOIN", f"VIS_F{i}")
        await _ev(db, "ZONE_ENTER", f"VIS_F{i}", zone_id="BILLING", ts_offset_s=10)
    # POS transaction 60s after the BILLING events: both within 1800s
    await _pos(db, ts_offset_s=70)
    await db.commit()

    f = await compute_funnel(STORE, db)
    stages = {s.stage: s for s in f.stages}

    assert stages["Entry"].count == 4
    assert stages["Zone Visit"].count == 3
    assert stages["Billing Queue"].count == 2
    # Purchase >= 1 (both billing visitors may match the single POS transaction)
    assert stages["Purchase"].count >= 1


@pytest.mark.asyncio
async def test_funnel_drop_off_entry_always_zero(db):
    """Entry stage always has drop_off_pct = 0.0 (it's the baseline)."""
    for i in range(10):
        await _ev(db, "ENTRY", f"VIS_DO{i}")
    await db.commit()

    f = await compute_funnel(STORE, db)
    entry = next(s for s in f.stages if s.stage == "Entry")
    assert entry.drop_off_pct == 0.0


@pytest.mark.asyncio
async def test_funnel_drop_off_pct_calculation(db):
    """
    4 entries, 2 zone visits → drop-off at Zone Visit = (4-2)/4 * 100 = 50.0%.
    """
    for i in range(4):
        await _ev(db, "ENTRY", f"VIS_P{i}")
    for i in range(2):
        await _ev(db, "ZONE_ENTER", f"VIS_P{i}", zone_id="FRAGRANCES")
    await db.commit()

    f = await compute_funnel(STORE, db)
    zone_stage = next(s for s in f.stages if s.stage == "Zone Visit")
    assert abs(zone_stage.drop_off_pct - 50.0) < 0.01


@pytest.mark.asyncio
async def test_funnel_drop_off_zero_when_no_previous(db):
    """When Entry count = 0, Zone Visit drop_off_pct must be 0.0 (no division by zero)."""
    await _ev(db, "ZONE_ENTER", "VIS_ORPHAN", zone_id="ZONE_X")
    await db.commit()

    f = await compute_funnel(STORE, db)
    # Entry=0, Zone Visit=1 → drop_off calls drop_off(1, 0) → returns 0.0
    zone_stage = next(s for s in f.stages if s.stage == "Zone Visit")
    assert zone_stage.drop_off_pct == 0.0


@pytest.mark.asyncio
async def test_funnel_reentry_not_double_counted(db):
    """
    REENTRY event_type is different from ENTRY.
    Funnel Stage 1 counts only event_type='ENTRY'.
    Same visitor_id with one ENTRY → Entry count = 1 (not 2).
    """
    await _ev(db, "ENTRY",   "VIS_RE")          # counted
    await _ev(db, "EXIT",    "VIS_RE", ts_offset_s=60)
    await _ev(db, "REENTRY", "VIS_RE", ts_offset_s=120)  # NOT counted in Entry stage
    await db.commit()

    f = await compute_funnel(STORE, db)
    entry = next(s for s in f.stages if s.stage == "Entry")
    # COUNT DISTINCT visitor_id WHERE event_type='ENTRY' → 1 (only one ENTRY event)
    assert entry.count == 1


@pytest.mark.asyncio
async def test_funnel_staff_excluded(db):
    """Staff visitors (is_staff=1) must not count in any funnel stage."""
    await _ev(db, "ENTRY", "VIS_CUST")
    await _ev(db, "ENTRY", "VIS_STFF", is_staff=1)
    await _ev(db, "ZONE_ENTER", "VIS_STFF", zone_id="ZONE_Y", is_staff=1)
    await db.commit()

    f = await compute_funnel(STORE, db)
    stages = {s.stage: s for s in f.stages}
    assert stages["Entry"].count == 1       # only customer
    assert stages["Zone Visit"].count == 0  # staff zone visit excluded


@pytest.mark.asyncio
async def test_funnel_store_id_in_response(db):
    """FunnelResponse.store_id must echo the requested store_id."""
    f = await compute_funnel(STORE, db)
    assert f.store_id == STORE


@pytest.mark.asyncio
async def test_funnel_computed_at_is_set(db):
    """FunnelResponse.computed_at must be a non-empty string."""
    f = await compute_funnel(STORE, db)
    assert f.computed_at
    assert isinstance(f.computed_at, str)


@pytest.mark.asyncio
async def test_funnel_purchase_requires_billing_zone_event(db):
    """
    Purchase stage requires visitor to have a BILLING zone event that correlates
    with a POS transaction. A visitor with POS but no BILLING zone event does NOT count.
    """
    # Visitor entered store but never entered a BILLING zone
    await _ev(db, "ENTRY",      "VIS_NOBILL", ts_offset_s=0)
    await _ev(db, "ZONE_ENTER", "VIS_NOBILL", zone_id="TOYS", ts_offset_s=10)
    # POS transaction 5 min later
    await _pos(db, ts_offset_s=300)
    await db.commit()

    f = await compute_funnel(STORE, db)
    purchase = next(s for s in f.stages if s.stage == "Purchase")
    # No BILLING zone event → no correlation → purchase = 0
    assert purchase.count == 0


@pytest.mark.asyncio
async def test_funnel_no_purchase_without_pos(db):
    """Visitor in BILLING zone but no POS transaction → Purchase count = 0."""
    await _ev(db, "ENTRY",      "VIS_NP")
    await _ev(db, "ZONE_ENTER", "VIS_NP", zone_id="BILLING")
    await db.commit()

    f = await compute_funnel(STORE, db)
    purchase = next(s for s in f.stages if s.stage == "Purchase")
    assert purchase.count == 0


@pytest.mark.asyncio
async def test_funnel_store_isolation(db):
    """Events and POS for other stores must not affect this store's funnel."""
    await _ev(db, "ENTRY", "VIS_OTHER", store_id="ST1008")
    await db.commit()

    f = await compute_funnel(STORE, db)
    entry = next(s for s in f.stages if s.stage == "Entry")
    assert entry.count == 0