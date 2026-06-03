from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import AnomalyResponse, Anomaly

logger = logging.getLogger(__name__)

# Rule-based thresholds (no historical baseline available)
QUEUE_WARN_DEPTH     = 3
QUEUE_CRITICAL_DEPTH = 6
CONVERSION_WARN_PCT  = 0.10   # below 10% is anomalous
DEAD_ZONE_MINUTES    = 30     # no zone visits in 30 min


async def compute_anomalies(store_id: str, db: AsyncSession) -> AnomalyResponse:
    """Detect active operational anomalies for a store."""
    now_utc = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    # --- Queue spike ---
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

    if queue_depth >= QUEUE_CRITICAL_DEPTH:
        anomalies.append(Anomaly(
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity="CRITICAL",
            description=f"Billing queue depth is {queue_depth} — critically high.",
            suggested_action="Open additional billing counters immediately.",
            detected_at=now_utc.isoformat(),
        ))
    elif queue_depth >= QUEUE_WARN_DEPTH:
        anomalies.append(Anomaly(
            anomaly_type="BILLING_QUEUE_SPIKE",
            severity="WARN",
            description=f"Billing queue depth is {queue_depth} — above threshold.",
            suggested_action="Consider opening an additional billing counter.",
            detected_at=now_utc.isoformat(),
        ))

    # --- Conversion drop ---
    result = await db.execute(
        text("SELECT COUNT(DISTINCT visitor_id) FROM events WHERE store_id=:sid AND is_staff=0"),
        {"sid": store_id},
    )
    total_visitors: int = result.scalar() or 0

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
    converted: int = result.scalar() or 0

    if total_visitors > 10:   # only flag when there is enough data
        rate = converted / total_visitors
        if rate < CONVERSION_WARN_PCT:
            anomalies.append(Anomaly(
                anomaly_type="CONVERSION_DROP",
                severity="WARN",
                description=f"Conversion rate is {rate:.1%} ({converted}/{total_visitors}) — below 10% threshold.",
                suggested_action="Check floor staff presence and product availability in high-dwell zones.",
                detected_at=now_utc.isoformat(),
            ))

    # --- Dead zone (no ZONE_ENTER in last 30 min) ---
    cutoff = now_utc.replace(tzinfo=timezone.utc)
    cutoff_iso = (cutoff.replace(microsecond=0).isoformat()
                  .replace("+00:00", "Z"))

    result = await db.execute(
        text("""
            SELECT DISTINCT zone_id
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ZONE_ENTER'
              AND zone_id IS NOT NULL
              AND zone_id NOT LIKE '%BILLING%'
              AND is_staff = 0
        """),
        {"sid": store_id},
    )
    all_zones = {row[0] for row in result.fetchall()}

    result = await db.execute(
        text("""
            SELECT DISTINCT zone_id
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ZONE_ENTER'
              AND zone_id IS NOT NULL
              AND zone_id NOT LIKE '%BILLING%'
              AND is_staff = 0
              AND datetime(timestamp) >= datetime(:cutoff, :offset)
        """),
        {"sid": store_id, "cutoff": cutoff_iso, "offset": f"-{DEAD_ZONE_MINUTES} minutes"},
    )
    active_zones = {row[0] for row in result.fetchall()}
    dead_zones = all_zones - active_zones

    for zone_id in sorted(dead_zones):
        anomalies.append(Anomaly(
            anomaly_type="DEAD_ZONE",
            severity="INFO",
            description=f"Zone {zone_id} has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
            suggested_action="Verify zone display is stocked and well-lit; consider repositioning signage.",
            detected_at=now_utc.isoformat(),
        ))

    logger.debug("Anomalies store=%s count=%d", store_id, len(anomalies))
    return AnomalyResponse(
        store_id=store_id,
        anomalies=anomalies,
        computed_at=now_utc.isoformat(),
    )