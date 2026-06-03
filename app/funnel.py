from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)


async def compute_funnel(store_id: str, db: AsyncSession) -> FunnelResponse:
    """4-stage conversion funnel. Session is the unit — re-entries do not double-count."""
    now_utc = datetime.now(timezone.utc).isoformat()

    # Stage 1: unique customer visitors who entered (ENTRY events)
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ENTRY'
              AND is_staff = 0
        """),
        {"sid": store_id},
    )
    entry_count: int = result.scalar() or 0

    # Stage 2: visitors who entered at least one named product zone
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ZONE_ENTER'
              AND zone_id IS NOT NULL
              AND zone_id NOT LIKE '%BILLING%'
              AND is_staff = 0
        """),
        {"sid": store_id},
    )
    zone_count: int = result.scalar() or 0

    # Stage 3: visitors who joined the billing queue
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
        """),
        {"sid": store_id},
    )
    queue_count: int = result.scalar() or 0

    # Stage 4: visitors converted via POS correlation (same logic as metrics)
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
    purchase_count: int = result.scalar() or 0

    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        lost = previous - current
        return round((lost / previous) * 100, 2)

    stages = [
        FunnelStage(stage="Entry",        count=entry_count,    drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit",   count=zone_count,     drop_off_pct=drop_off(zone_count,     entry_count)),
        FunnelStage(stage="Billing Queue",count=queue_count,    drop_off_pct=drop_off(queue_count,    zone_count)),
        FunnelStage(stage="Purchase",     count=purchase_count, drop_off_pct=drop_off(purchase_count, queue_count)),
    ]

    logger.debug("Funnel store=%s stages=%s", store_id, [s.count for s in stages])
    return FunnelResponse(store_id=store_id, stages=stages, computed_at=now_utc)