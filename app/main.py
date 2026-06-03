from __future__ import annotations
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models import IngestRequest, IngestResponse
from app.ingestion import ingest_events, load_pos_transactions
from app.metrics import compute_metrics
from app.funnel import compute_funnel
from app.heatmap import compute_heatmap
from app.anomalies import compute_anomalies
from app.health import compute_health

# --- Logging setup ---
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

# --- Database ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./storage/store_intelligence.db")
POS_CSV      = os.getenv("POS_CSV", "./data/pos_transactions.csv")
SCHEMA_SQL   = "./storage/schema.sql"

engine         = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply schema on startup
    async with engine.begin() as conn:
        with open(SCHEMA_SQL, "r") as f:
            for stmt in f.read().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(text(stmt))
    logger.info("DB schema applied")

    # Load POS CSV on startup
    async with AsyncSessionLocal() as db:
        loaded = await load_pos_transactions(POS_CSV, db)
        logger.info("POS CSV loaded transactions=%d", loaded)

    yield
    await engine.dispose()


app = FastAPI(title="Store Intelligence API", lifespan=lifespan)


# --- Request logging middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id   = str(uuid.uuid4())[:8]
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    store_id = request.path_params.get("store_id", "")
    logger.info(
        '{"trace_id":"%s","store_id":"%s","endpoint":"%s","latency_ms":%s,"status_code":%d}',
        trace_id, store_id, request.url.path, latency_ms, response.status_code,
    )
    return response


# --- Graceful DB error handler ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=503,
        content={"error": "Service temporarily unavailable", "detail": str(exc)},
    )


# --- Routes ---

@app.post("/events/ingest", response_model=IngestResponse)
async def ingest(payload: IngestRequest, db: AsyncSession = Depends(get_db)):
    """Batch ingest up to 500 events. Idempotent by event_id."""
    return await ingest_events(payload.events, db)


@app.get("/stores/{store_id}/metrics")
async def metrics(store_id: str, db: AsyncSession = Depends(get_db)):
    """Real-time store metrics: visitors, conversion rate, dwell, queue."""
    return await compute_metrics(store_id, db)


@app.get("/stores/{store_id}/funnel")
async def funnel(store_id: str, db: AsyncSession = Depends(get_db)):
    """4-stage conversion funnel with drop-off percentages."""
    return await compute_funnel(store_id, db)


@app.get("/stores/{store_id}/heatmap")
async def heatmap(store_id: str, db: AsyncSession = Depends(get_db)):
    """Zone visit frequency + avg dwell, normalised 0-100."""
    return await compute_heatmap(store_id, db)


@app.get("/stores/{store_id}/anomalies")
async def anomalies(store_id: str, db: AsyncSession = Depends(get_db)):
    """Active anomalies: queue spike, conversion drop, dead zone."""
    return await compute_anomalies(store_id, db)


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Service health: DB connectivity + per-camera feed staleness."""
    return await compute_health(db)