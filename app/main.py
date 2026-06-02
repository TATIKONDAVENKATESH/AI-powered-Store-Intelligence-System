from __future__ import annotations
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models import (
    IngestRequest, IngestResponse,
    MetricsResponse, FunnelResponse, HeatmapResponse, AnomalyResponse, HealthResponse,
    HeatmapZone,
)
from app.ingestion import ingest_events, load_pos_transactions
from app.metrics import compute_metrics
from app.funnel import compute_funnel
from app.anomalies import compute_anomalies
from app.health import compute_health

load_dotenv()

# --- Logging setup ---
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

# --- DB setup ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./storage/store_intelligence.db")
ASYNC_DB_URL = DATABASE_URL.replace("sqlite:///", "sqlite+aiosqlite:///")
engine = create_async_engine(ASYNC_DB_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
POS_CSV = os.getenv("POS_CSV", "./data/pos_transactions.csv")


async def init_db() -> None:
    """Create tables from schema.sql if they do not exist."""
    with open(SCHEMA_PATH, "r") as f:
        schema = f.read()
    async with engine.begin() as conn:
        for statement in schema.split(";"):
            stmt = statement.strip()
            if stmt:
                await conn.execute(text(stmt))
    logger.info("Database schema initialised")


async def load_pos_on_startup() -> None:
    """Load POS CSV into DB on startup (idempotent)."""
    if not os.path.exists(POS_CSV):
        logger.warning("POS CSV not found at %s — skipping", POS_CSV)
        return
    async with SessionLocal() as db:
        loaded = await load_pos_transactions(POS_CSV, db)
    logger.info("POS startup load: %d new transactions", loaded)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await load_pos_on_startup()
    yield
    await engine.dispose()


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Dependency: DB session ---
async def get_db():
    async with SessionLocal() as session:
        yield session


# --- Request logging middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    start = time.time()
    response: Response = await call_next(request)
    latency_ms = round((time.time() - start) * 1000, 1)
    logger.info(
        "trace_id=%s method=%s path=%s status=%d latency_ms=%.1f",
        trace_id, request.method, request.url.path, response.status_code, latency_ms,
    )
    return response


# --- Exception handler: no raw stack traces in responses ---
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc)},
    )


# --- Routes ---

@app.post("/events/ingest", response_model=IngestResponse)
async def ingest(payload: IngestRequest, db: AsyncSession = Depends(get_db)):
    """Ingest a batch of detection events. Idempotent by event_id. Max 500 per call."""
    logger.info("Ingest request event_count=%d", len(payload.events))
    return await ingest_events(payload.events, db)


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def get_metrics(store_id: str, db: AsyncSession = Depends(get_db)):
    """Return real-time store metrics: visitors, conversion rate, dwell, queue."""
    try:
        return await compute_metrics(store_id, db)
    except Exception as exc:
        logger.error("Metrics error store=%s: %s", store_id, exc)
        raise HTTPException(status_code=503, detail="Metrics computation failed")


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def get_funnel(store_id: str, db: AsyncSession = Depends(get_db)):
    """Return conversion funnel: Entry → Zone Visit → Billing Queue → Purchase."""
    try:
        return await compute_funnel(store_id, db)
    except Exception as exc:
        logger.error("Funnel error store=%s: %s", store_id, exc)
        raise HTTPException(status_code=503, detail="Funnel computation failed")


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def get_heatmap(store_id: str, db: AsyncSession = Depends(get_db)):
    """Return zone visit frequency and dwell, normalised 0-100."""
    try:
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc).isoformat()

        result = await db.execute(
            text("""
                SELECT
                    zone_id,
                    COUNT(DISTINCT visitor_id) AS visit_freq,
                    AVG(dwell_ms) / 1000.0 AS avg_dwell_s
                FROM events
                WHERE store_id = :sid
                  AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
                  AND zone_id IS NOT NULL
                  AND is_staff = 0
                GROUP BY zone_id
            """),
            {"sid": store_id},
        )
        rows = result.fetchall()

        if not rows:
            return HeatmapResponse(store_id=store_id, zones=[], computed_at=now_utc)

        # Normalise visit frequency to 0-100
        max_freq = max(r[1] for r in rows) or 1
        zones = [
            HeatmapZone(
                zone_id=r[0],
                sku_zone=r[0],
                visit_frequency=r[1],
                avg_dwell_seconds=round(r[2] or 0.0, 2),
                normalised_score=round((r[1] / max_freq) * 100, 1),
                data_confidence=r[1] >= 20,  # False if fewer than 20 sessions
            )
            for r in rows
        ]
        return HeatmapResponse(store_id=store_id, zones=zones, computed_at=now_utc)

    except Exception as exc:
        logger.error("Heatmap error store=%s: %s", store_id, exc)
        raise HTTPException(status_code=503, detail="Heatmap computation failed")


@app.get("/stores/{store_id}/anomalies", response_model=AnomalyResponse)
async def get_anomalies(store_id: str, db: AsyncSession = Depends(get_db)):
    """Return active operational anomalies with severity and suggested actions."""
    try:
        return await compute_anomalies(store_id, db)
    except Exception as exc:
        logger.error("Anomalies error store=%s: %s", store_id, exc)
        raise HTTPException(status_code=503, detail="Anomaly detection failed")


@app.get("/health", response_model=HealthResponse)
async def get_health(db: AsyncSession = Depends(get_db)):
    """Return service health, per-camera feed status, and DB connectivity."""
    return await compute_health(db)