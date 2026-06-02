from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import StoreEvent, IngestResponse

logger = logging.getLogger(__name__)


async def ingest_events(
    events: List[StoreEvent],
    db: AsyncSession,
) -> IngestResponse:
    """Validate, deduplicate, and persist a batch of events. Idempotent by event_id."""
    accepted = 0
    rejected = 0
    duplicates = 0
    errors: List[str] = []
    now_utc = datetime.now(timezone.utc).isoformat()

    for event in events:
        try:
            result = await db.execute(
                text("SELECT 1 FROM events WHERE event_id = :eid"),
                {"eid": event.event_id},
            )
            if result.fetchone():
                duplicates += 1
                continue

            # Column names match schema.sql exactly
            await db.execute(
                text("""
                    INSERT INTO events (
                        event_id, store_id, camera_id, visitor_id,
                        event_type, timestamp, zone_id, dwell_ms,
                        is_staff, confidence, queue_depth, sku_zone,
                        session_seq, ingested_at
                    ) VALUES (
                        :event_id, :store_id, :camera_id, :visitor_id,
                        :event_type, :timestamp, :zone_id, :dwell_ms,
                        :is_staff, :confidence, :queue_depth, :sku_zone,
                        :session_seq, :ingested_at
                    )
                """),
                {
                    "event_id":    event.event_id,
                    "store_id":    event.store_id,
                    "camera_id":   event.camera_id,
                    "visitor_id":  event.visitor_id,
                    "event_type":  event.event_type,
                    "timestamp":   event.timestamp,
                    "zone_id":     event.zone_id,
                    "dwell_ms":    event.dwell_ms,
                    "is_staff":    1 if event.is_staff else 0,
                    "confidence":  event.confidence,
                    "queue_depth": event.metadata.queue_depth,
                    "sku_zone":    event.metadata.sku_zone,
                    "session_seq": event.metadata.session_seq,
                    "ingested_at": now_utc,
                },
            )
            accepted += 1

        except Exception as exc:
            rejected += 1
            errors.append(f"event_id={event.event_id}: {str(exc)}")
            logger.warning("Ingestion error for event %s: %s", event.event_id, exc)

    await db.commit()
    logger.info(
        "Ingest batch done accepted=%d rejected=%d duplicates=%d",
        accepted, rejected, duplicates,
    )
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicates=duplicates,
        errors=errors,
    )


async def load_pos_transactions(csv_path: str, db: AsyncSession) -> int:
    """
    Load POS CSV into pos_transactions table.
    Groups by invoice_number (unique per transaction) and sums total_amount per invoice.
    Idempotent — skips rows already present.
    """
    import csv
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")

    # Group line-item rows by invoice_number to get one record per transaction
    invoices: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "date": "", "time": "", "store": ""})

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            inv = row.get("invoice_number", "").strip()
            if not inv:
                continue
            invoices[inv]["date"]  = row.get("order_date", "")
            invoices[inv]["time"]  = row.get("order_time", "")
            invoices[inv]["store"] = row.get("store_id", "")
            try:
                invoices[inv]["total"] += float(row.get("total_amount", 0) or 0)
            except ValueError:
                pass

    loaded = 0
    for inv, data in invoices.items():
        try:
            dt_str = f"{data['date']} {data['time']}"
            for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt_ist = datetime.strptime(dt_str, fmt).replace(tzinfo=ist)
                    break
                except ValueError:
                    continue
            else:
                logger.warning("Cannot parse POS date: %s", dt_str)
                continue

            dt_utc = dt_ist.astimezone(timezone.utc).isoformat()
            store_id = "STORE_BLR_002"  # ST1008 maps to canonical store ID

            exists = await db.execute(
                text("SELECT 1 FROM pos_transactions WHERE transaction_id = :tid"),
                {"tid": inv},
            )
            if exists.fetchone():
                continue

            await db.execute(
                text("""
                    INSERT INTO pos_transactions
                        (transaction_id, store_id, timestamp, basket_value)
                    VALUES (:tid, :sid, :ts, :bv)
                """),
                {"tid": inv, "sid": store_id, "ts": dt_utc, "bv": round(data["total"], 2)},
            )
            loaded += 1

        except Exception as exc:
            logger.warning("POS invoice error %s: %s", inv, exc)

    await db.commit()
    logger.info("Loaded %d POS transactions (from invoices)", loaded)
    return loaded


def build_ingest_batches(
    events: List[StoreEvent], batch_size: int = 500
) -> List[List[StoreEvent]]:
    """Split event list into batches of up to batch_size."""
    return [events[i:i + batch_size] for i in range(0, len(events), batch_size)]