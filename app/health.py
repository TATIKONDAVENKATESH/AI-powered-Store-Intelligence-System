from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import HealthResponse, CameraFeedStatus

logger = logging.getLogger(__name__)

# All cameras in this store (from store_layout.json)
ALL_CAMERAS = [
    "CAM_ENTRY_01",
    "CAM_FLOOR_A",
    "CAM_FLOOR_B",
    "CAM_BILLING_01",
    "CAM_STAFF_01",
]

STALE_THRESHOLD_MINUTES = 10  # feed is stale if no events in last 10 min


async def compute_health(db: AsyncSession) -> HealthResponse:
    """Check DB connectivity and per-camera feed staleness."""
    now_utc = datetime.now(timezone.utc)
    checked_at = now_utc.isoformat()
    stale_cutoff = (now_utc - timedelta(minutes=STALE_THRESHOLD_MINUTES)).isoformat()

    db_connected = True
    store_feeds: list[CameraFeedStatus] = []
    last_event_at: str | None = None

    try:
        # Get last event timestamp per camera
        result = await db.execute(
            text("""
                SELECT camera_id, MAX(timestamp) AS last_ts
                FROM events
                GROUP BY camera_id
            """)
        )
        camera_last: dict[str, str] = {row[0]: row[1] for row in result.fetchall()}

        # Get overall last event timestamp
        result = await db.execute(text("SELECT MAX(timestamp) FROM events"))
        last_event_at = result.scalar()

        for cam_id in ALL_CAMERAS:
            last_ts = camera_last.get(cam_id)
            # Stale if never seen or last event is older than threshold
            stale = (last_ts is None) or (last_ts < stale_cutoff)
            store_feeds.append(CameraFeedStatus(
                camera_id=cam_id,
                last_event_at=last_ts,
                stale=stale,
            ))

    except Exception as exc:
        logger.error("Health check DB error: %s", exc)
        db_connected = False
        # Return all cameras as stale when DB is unreachable
        store_feeds = [
            CameraFeedStatus(camera_id=cam, last_event_at=None, stale=True)
            for cam in ALL_CAMERAS
        ]

    any_stale = any(f.stale for f in store_feeds)

    if not db_connected:
        status = "down"
    elif any_stale:
        status = "degraded"
    else:
        status = "ok"

    return HealthResponse(
        status=status,
        store_feeds=store_feeds,
        last_event_at=last_event_at,
        stale_feed=any_stale,
        db_connected=db_connected,
        checked_at=checked_at,
    )