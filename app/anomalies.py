from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Anomaly, AnomalyResponse

logger = logging.getLogger(__name__)

# Thresholds
QUEUE_SPIKE_THRESHOLD = 3       # queue depth above this = spike
CONVERSION_DROP_THRESHOLD = 0.10  # conversion rate below this = drop
DEAD_ZONE_MINUTES = 30          # no zone visits in this window = dead zone


async def compute_anomalies(store_id: str, db: AsyncSession) -> AnomalyResponse:
    """Detect active operational anomalies. Returns list of current anomalies."""
    now_utc = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    # --- Anomaly 1: BILLING_QUEUE_SPIKE ---
    # Count visitors currently in billing queue (joined but not exited/abandoned)
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

    if queue_depth > QUEUE_SPIKE_THRESHOLD:
        anomalies.append(Anomaly(
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity="CRITICAL" if queue_depth > QUEUE_SPIKE_THRESHOLD * 2 else "WARN",
            description=f"Billing queue depth is {queue_depth} — above threshold of {QUEUE_SPIKE_THRESHOLD}.",
            suggested_action="Deploy additional billing staff immediately to clear the queue.",
            detected_at=now_utc.isoformat(),
        ))

    # --- Anomaly 2: CONVERSION_DROP ---
    # Compute current conversion rate and flag if below threshold
    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :sid AND event_type = 'ENTRY' AND is_staff = 0
        """),
        {"sid": store_id},
    )
    unique_visitors: int = result.scalar() or 0

    result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT e.visitor_id)
            FROM events e
            INNER JOIN pos_transactions p
                ON p.store_id = e.store_id
               AND datetime(p.timestamp) >= datetime(e.timestamp)
               AND datetime(p.timestamp) <= datetime(e.timestamp, '+300 seconds')
            WHERE e.store_id = :sid AND e.zone_id = 'BILLING' AND e.is_staff = 0
        """),
        {"sid": store_id},
    )
    converted: int = result.scalar() or 0

    if unique_visitors > 0:
        conversion_rate = converted / unique_visitors
        if conversion_rate < CONVERSION_DROP_THRESHOLD:
            anomalies.append(Anomaly(
                anomaly_type="CONVERSION_DROP",
                severity="WARN",
                description=(
                    f"Conversion rate is {conversion_rate:.1%} — below threshold of "
                    f"{CONVERSION_DROP_THRESHOLD:.0%}. "
                    f"{unique_visitors} visitors, only {converted} purchases."
                ),
                suggested_action="Review floor staff engagement and check for product availability issues.",
                detected_at=now_utc.isoformat(),
            ))

    # --- Anomaly 3: DEAD_ZONE ---
    # Any product zone with zero ZONE_ENTER events in the last 30 minutes
    window_start = (now_utc - timedelta(minutes=DEAD_ZONE_MINUTES)).isoformat()

    result = await db.execute(
        text("""
            SELECT DISTINCT zone_id
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ZONE_ENTER'
              AND is_staff = 0
              AND timestamp >= :window_start
              AND zone_id NOT IN ('ENTRY_LOBBY', 'STAFF_ROOM')
        """),
        {"sid": store_id, "window_start": window_start},
    )
    active_zones = {row[0] for row in result.fetchall()}

    # All product zones defined in the store (hardcoded from store_layout.json)
    all_product_zones = {
        "SKINCARE_TOP", "MAKEUP_CENTER", "FRAGRANCE_NAIL",
        "SKINCARE_BOTTOM", "BILLING",
    }
    dead_zones = all_product_zones - active_zones

    for zone_id in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_type="DEAD_ZONE",
            severity="INFO",
            description=f"Zone '{zone_id}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
            suggested_action=f"Send a staff member to check zone '{zone_id}' for restocking or display issues.",
            detected_at=now_utc.isoformat(),
        ))

    logger.debug("Anomalies store=%s count=%d", store_id, len(anomalies))

    return AnomalyResponse(
        store_id=store_id,
        anomalies=anomalies,
        computed_at=now_utc.isoformat(),
    )