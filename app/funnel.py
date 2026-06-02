from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)


async def compute_funnel(store_id: str, db: AsyncSession) -> FunnelResponse:
    """
    Compute conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    Session is the unit — re-entries do NOT create a new session for the same visitor_id.
    Staff excluded throughout.
    """
    now_utc = datetime.now(timezone.utc).isoformat()

    # Stage 1: Unique visitors who entered (ENTRY events, staff excluded)
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
    total_entered: int = result.scalar() or 0

    # Stage 2: Unique visitors who visited at least one product zone
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
              AND zone_id NOT IN ('ENTRY_LOBBY', 'STAFF_ROOM')
              AND zone_id IS NOT NULL
              AND is_staff = 0
        """),
        {"sid": store_id},
    )
    visited_zone: int = result.scalar() or 0

    # Stage 3: Unique visitors who joined the billing queue
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
    reached_billing: int = result.scalar() or 0

    # Stage 4: Unique visitors who completed a purchase
    # Visitor was in billing zone within 5 min before a POS transaction
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT e.visitor_id)
            FROM events e
            INNER JOIN pos_transactions p
                ON p.store_id = e.store_id
               AND datetime(p.timestamp) >= datetime(e.timestamp)
               AND datetime(p.timestamp) <= datetime(e.timestamp, '+300 seconds')
            WHERE e.store_id = :sid
              AND e.zone_id = 'BILLING'
              AND e.is_staff = 0
        """),
        {"sid": store_id},
    )
    purchased: int = result.scalar() or 0

    # Build stages with drop-off percentages relative to the previous stage
    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        lost = previous - current
        return round((lost / previous) * 100, 2)

    stages = [
        FunnelStage(
            stage="Entry",
            count=total_entered,
            drop_off_pct=0.0,                               # baseline stage
        ),
        FunnelStage(
            stage="Zone Visit",
            count=visited_zone,
            drop_off_pct=drop_off(visited_zone, total_entered),
        ),
        FunnelStage(
            stage="Billing Queue",
            count=reached_billing,
            drop_off_pct=drop_off(reached_billing, visited_zone),
        ),
        FunnelStage(
            stage="Purchase",
            count=purchased,
            drop_off_pct=drop_off(purchased, reached_billing),
        ),
    ]

    logger.debug(
        "Funnel store=%s entry=%d zone=%d billing=%d purchase=%d",
        store_id, total_entered, visited_zone, reached_billing, purchased,
    )

    return FunnelResponse(
        store_id=store_id,
        stages=stages,
        computed_at=now_utc,
    )