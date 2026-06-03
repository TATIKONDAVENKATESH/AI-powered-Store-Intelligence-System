from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import HealthResponse, CameraFeedStatus

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MINUTES = 10


async def compute_health(db: AsyncSession) -> HealthResponse:
    """DB connectivity + per-camera feed staleness. STALE_FEED warning if >10 min lag."""
    now_utc    = datetime.now(timezone.utc)
    checked_at = now_utc.isoformat()

    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        return HealthResponse(
            status="down",
            store_feeds=[],
            last_event_at=None,
            stale_feed=True,
            db_connected=False,
            checked_at=checked_at,
        )

    # Per-camera last event timestamp
    result = await db.execute(
        text("""
            SELECT camera_id, MAX(timestamp) AS last_ts
            FROM events
            GROUP BY camera_id
        """)
    )
    cam_rows = result.fetchall()

    stale_cutoff = now_utc - timedelta(minutes=STALE_THRESHOLD_MINUTES)
    store_feeds: list[CameraFeedStatus] = []
    any_stale = False

    for cam_id, last_ts in cam_rows:
        is_stale = True
        if last_ts:
            try:
                ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                is_stale = ts < stale_cutoff
            except ValueError:
                pass
        if is_stale:
            any_stale = True
        store_feeds.append(CameraFeedStatus(
            camera_id=cam_id,
            last_event_at=last_ts,
            stale=is_stale,
        ))

    # Overall last event across all cameras
    result = await db.execute(text("SELECT MAX(timestamp) FROM events"))
    global_last = result.scalar()

    status = "ok" if not any_stale else "degraded"

    return HealthResponse(
        status=status,
        store_feeds=store_feeds,
        last_event_at=global_last,
        stale_feed=any_stale,
        db_connected=True,
        checked_at=checked_at,
    )