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

_IST = ZoneInfo("Asia/Kolkata")  # POS CSV timestamps are in IST


async def ingest_events(
    events: List[StoreEvent],
    db: AsyncSession,
) -> IngestResponse:
    """Validate, deduplicate, and persist a batch of events. Idempotent by event_id."""
    accepted   = 0
    rejected   = 0
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
    Load POS CSV into pos_transactions table. Idempotent — skips already-loaded rows.

    Actual POS CSV columns (from sample file):
      order_id, order_date, order_time, store_id, product_id, brand_name, total_amount

    One row per SKU — we aggregate by (store_id + order_date + order_time) composite key
    since there is no invoice_number in this dataset.

    Dates are DD-MM-YYYY in IST → converted to UTC ISO-8601.
    """
    orders: dict[str, dict] = defaultdict(
        lambda: {"total": 0.0, "date": "", "time": "", "store_id": ""}
    )

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                order_date   = row.get("order_date", "").strip()
                order_time   = row.get("order_time", "").strip()
                store_id_raw = row.get("store_id", "").strip()

                # Use invoice_number if present (richer test CSVs), else composite key
                invoice = row.get("invoice_number", "").strip()
                if invoice:
                    txn_key = invoice
                elif store_id_raw and order_date and order_time:
                    txn_key = f"{store_id_raw}_{order_date}_{order_time}"
                else:
                    continue  # skip rows with insufficient identifying info

                orders[txn_key]["date"]     = order_date
                orders[txn_key]["time"]     = order_time
                orders[txn_key]["store_id"] = store_id_raw
                try:
                    orders[txn_key]["total"] += float(row.get("total_amount", 0) or 0)
                except ValueError:
                    pass  # non-numeric total_amount — amount stays at 0

    except FileNotFoundError:
        logger.warning("POS CSV not found at %s — skipping", csv_path)
        return 0

    loaded = 0
    for txn_key, data in orders.items():
        try:
            dt_str = f"{data['date']} {data['time']}"
            dt_ist = None
            for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M"):
                try:
                    dt_ist = datetime.strptime(dt_str, fmt).replace(tzinfo=_IST)
                    break
                except ValueError:
                    continue

            if dt_ist is None:
                logger.warning("Cannot parse POS date/time: %s", dt_str)
                continue

            dt_utc   = dt_ist.astimezone(timezone.utc).isoformat()
            store_id = data["store_id"] or "ST1008"

            exists = await db.execute(
                text("SELECT 1 FROM pos_transactions WHERE transaction_id = :tid"),
                {"tid": txn_key},
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
                    "tid": txn_key,
                    "sid": store_id,
                    "ts":  dt_utc,
                    "bv":  round(data["total"], 2),
                },
            )
            loaded += 1

        except Exception as exc:
            logger.warning("POS order error %s: %s", txn_key, exc)

    await db.commit()
    logger.info("Loaded %d POS transactions", loaded)
    return loaded


def build_ingest_batches(
    events: List[StoreEvent], batch_size: int = 500
) -> List[List[StoreEvent]]:
    """Split event list into batches of at most batch_size."""
    return [events[i:i + batch_size] for i in range(0, len(events), batch_size)]