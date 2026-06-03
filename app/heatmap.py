from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import HeatmapResponse, HeatmapZone

logger = logging.getLogger(__name__)

MIN_SESSIONS_FOR_CONFIDENCE = 20


async def compute_heatmap(store_id: str, db: AsyncSession) -> HeatmapResponse:
    """Zone visit frequency + avg dwell, normalised 0-100."""
    now_utc = datetime.now(timezone.utc).isoformat()

    result = await db.execute(
        text("""
            SELECT
                zone_id,
                sku_zone,
                COUNT(DISTINCT visitor_id)   AS visit_freq,
                AVG(dwell_ms) / 1000.0       AS avg_dwell_s
            FROM events
            WHERE store_id = :sid
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
              AND zone_id IS NOT NULL
              AND is_staff = 0
            GROUP BY zone_id, sku_zone
        """),
        {"sid": store_id},
    )
    rows = result.fetchall()

    if not rows:
        return HeatmapResponse(store_id=store_id, zones=[], computed_at=now_utc)

    max_freq = max(row[2] for row in rows) or 1  # avoid division by zero

    zones = []
    for row in rows:
        zone_id, sku_zone, freq, avg_dwell = row
        normalised = round((freq / max_freq) * 100, 2)
        zones.append(HeatmapZone(
            zone_id=zone_id,
            sku_zone=sku_zone,
            visit_frequency=freq,
            avg_dwell_seconds=round(avg_dwell or 0.0, 2),
            normalised_score=normalised,
            data_confidence=(freq >= MIN_SESSIONS_FOR_CONFIDENCE),
        ))

    # Sort by normalised_score descending for dashboard rendering
    zones.sort(key=lambda z: z.normalised_score, reverse=True)

    logger.debug("Heatmap store=%s zones=%d", store_id, len(zones))
    return HeatmapResponse(store_id=store_id, zones=zones, computed_at=now_utc)