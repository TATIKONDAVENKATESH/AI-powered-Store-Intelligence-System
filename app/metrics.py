from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import List
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import MetricsResponse, ZoneDwell

logger = logging.getLogger(__name__)


async def compute_metrics(store_id: str, db: AsyncSession) -> MetricsResponse:
    """Compute real-time store metrics. Excludes is_staff events throughout."""
    now_utc = datetime.now(timezone.utc).isoformat()

    # Unique customer visitors — distinct visitor_ids, staff excluded
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid AND is_staff = 0
        """),
        {"sid": store_id},
    )
    unique_visitors: int = result.scalar() or 0

    # Total POS transactions for this store today
    result = await db.execute(
        text("SELECT COUNT(*) FROM pos_transactions WHERE store_id = :sid"),
        {"sid": store_id},
    )
    total_transactions: int = result.scalar() or 0

    # Conversion: visitor in any BILLING zone within 5 min before a POS transaction
    # zone_id LIKE '%BILLING%' covers ST1076_Z_BILLING_01 and ST1008_Z_BILLING_01
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT e.visitor_id)
            FROM events e
            INNER JOIN pos_transactions p
                ON  p.store_id = e.store_id
                AND datetime(p.timestamp) >= datetime(e.timestamp)
                AND datetime(p.timestamp) <= datetime(e.timestamp, '+300 seconds')
            WHERE e.store_id = :sid
              AND e.zone_id LIKE '%BILLING%'
              AND e.is_staff = 0
        """),
        {"sid": store_id},
    )
    converted_visitors: int = result.scalar() or 0

    conversion_rate = (
        round(converted_visitors / unique_visitors, 4) if unique_visitors > 0 else 0.0
    )

    # Average dwell per zone (seconds), customer events only
    result = await db.execute(
        text("""
            SELECT zone_id,
                   AVG(dwell_ms) / 1000.0 AS avg_dwell_s,
                   COUNT(*) AS visit_count
            FROM events
            WHERE store_id = :sid
              AND event_type IN ('ZONE_DWELL', 'ZONE_ENTER')
              AND zone_id IS NOT NULL
              AND is_staff = 0
            GROUP BY zone_id
        """),
        {"sid": store_id},
    )
    zone_rows = result.fetchall()
    avg_dwell_per_zone: List[ZoneDwell] = [
        ZoneDwell(
            zone_id=row[0],
            avg_dwell_seconds=round(row[1] or 0.0, 2),
            visit_count=row[2],
        )
        for row in zone_rows
    ]

    # Current queue depth — visitors with BILLING_QUEUE_JOIN but no EXIT or ABANDON
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
              AND visitor_id NOT IN (
                  SELECT DISTINCT visitor_id FROM events
                  WHERE store_id = :sid
                    AND event_type IN ('EXIT', 'BILLING_QUEUE_ABANDON')
              )
        """),
        {"sid": store_id},
    )
    queue_depth: int = result.scalar() or 0

    # Abandonment rate
    result = await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT CASE WHEN event_type='BILLING_QUEUE_JOIN'    THEN visitor_id END),
                COUNT(DISTINCT CASE WHEN event_type='BILLING_QUEUE_ABANDON' THEN visitor_id END)
            FROM events
            WHERE store_id = :sid AND is_staff = 0
        """),
        {"sid": store_id},
    )
    row = result.fetchone()
    joined    = row[0] or 0
    abandoned = row[1] or 0
    abandonment_rate = round(abandoned / joined, 4) if joined > 0 else 0.0

    logger.debug(
        "Metrics store=%s visitors=%d converted=%d rate=%.4f",
        store_id, unique_visitors, converted_visitors, conversion_rate,
    )

    return MetricsResponse(
        store_id=store_id,
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=queue_depth,
        abandonment_rate=abandonment_rate,
        total_transactions=total_transactions,
        computed_at=now_utc,
    )