from __future__ import annotations
import csv
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import StoreEvent, IngestResponse

logger = logging.getLogger(__name__)

# IST timezone for POS CSV date/time conversion
_IST = ZoneInfo("Asia/Kolkata")


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
    Groups product-level rows by order_id — each unique order_id is one transaction.
    POS CSV columns: order_id, order_date, order_time, store_id, product_id, brand_name, total_amount
    Dates are DD-MM-YYYY IST, converted to UTC ISO-8601.
    Idempotent — skips order_ids already in DB.
    """
    # Aggregate product-level rows into one record per order_id
    orders: dict[str, dict] = defaultdict(
        lambda: {"total": 0.0, "date": "", "time": "", "store_id": ""}
    )

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                order_id = row.get("order_id", "").strip()
                if not order_id:
                    continue
                # Last row for this order_id sets the date/time/store (all rows identical)
                orders[order_id]["date"]     = row.get("order_date", "").strip()
                orders[order_id]["time"]     = row.get("order_time", "").strip()
                orders[order_id]["store_id"] = row.get("store_id", "").strip()
                try:
                    orders[order_id]["total"] += float(row.get("total_amount", 0) or 0)
                except ValueError:
                    pass
    except FileNotFoundError:
        logger.warning("POS CSV not found at %s — skipping", csv_path)
        return 0

    loaded = 0
    for order_id, data in orders.items():
        try:
            dt_str = f"{data['date']} {data['time']}"
            # Parse DD-MM-YYYY HH:MM:SS (actual CSV format)
            for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt_ist = datetime.strptime(dt_str, fmt).replace(tzinfo=_IST)
                    break
                except ValueError:
                    continue
            else:
                logger.warning("Cannot parse POS date/time: %s", dt_str)
                continue

            dt_utc = dt_ist.astimezone(timezone.utc).isoformat()

            # Use store_id directly from CSV (ST1008, ST1076, etc.)
            store_id = data["store_id"] or "ST1008"

            exists = await db.execute(
                text("SELECT 1 FROM pos_transactions WHERE transaction_id = :tid"),
                {"tid": order_id},
            )
            if exists.fetchone():
                continue

            await db.execute(
                text("""
                    INSERT INTO pos_transactions
                        (transaction_id, store_id, timestamp, basket_value)
                    VALUES (:tid, :sid, :ts, :bv)
                """),
                {
                    "tid": order_id,
                    "sid": store_id,
                    "ts":  dt_utc,
                    "bv":  round(data["total"], 2),
                },
            )
            loaded += 1

        except Exception as exc:
            logger.warning("POS order error %s: %s", order_id, exc)

    await db.commit()
    logger.info("Loaded %d POS transactions", loaded)
    return loaded


def build_ingest_batches(
    events: List[StoreEvent], batch_size: int = 500
) -> List[List[StoreEvent]]:
    """Split event list into batches of at most batch_size."""
    return [events[i:i + batch_size] for i in range(0, len(events), batch_size)]