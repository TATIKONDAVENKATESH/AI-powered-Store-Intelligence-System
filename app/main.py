from __future__ import annotations
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, Depends, Path
from fastapi.exceptions import RequestValidationError
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

# ── Structured logging ────────────────────────────────────────────────────────
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=log_level,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────
# default must use sqlite+aiosqlite:// prefix for async SQLAlchemy
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./storage/store_intelligence.db",
)
POS_CSV    = os.getenv("POS_CSV", "./data/pos_transactions.csv")
SCHEMA_SQL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage", "schema.sql",
)

engine       = create_async_engine(DATABASE_URL, echo=False)
# Named SessionLocal so test_main.py can monkeypatch main_mod.SessionLocal
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """FastAPI dependency: yields an async DB session."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Apply schema and load POS CSV on startup; dispose engine on shutdown."""
    # Ensure storage/ directory exists (for local non-Docker run)
    os.makedirs("storage", exist_ok=True)

    async with engine.begin() as conn:
        with open(SCHEMA_SQL, "r") as f:
            for stmt in f.read().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(text(stmt))
    logger.info("DB schema applied")

    # Load POS CSV (idempotent — skips already-loaded rows)
    async with SessionLocal() as db:
        loaded = await load_pos_transactions(POS_CSV, db)
        logger.info("POS CSV loaded transactions=%d", loaded)

    yield
    await engine.dispose()


app = FastAPI(title="Store Intelligence API", version="1.0.0", lifespan=lifespan)


# ── Request logging middleware ────────────────────────────────────────────────
# Logs: trace_id, store_id, endpoint, latency_ms, event_count (ingest), status_code
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id   = str(uuid.uuid4())[:8]
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    store_id   = request.path_params.get("store_id", "")
    logger.info(
        '{"trace_id":"%s","store_id":"%s","endpoint":"%s","latency_ms":%s,"status_code":%d}',
        trace_id, store_id, request.url.path, latency_ms, response.status_code,
    )
    return response


# ── 422 validation error handler ─────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )


# ── 503 global exception handler (no raw stack traces) ───────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=503,
        content={"error": "Service temporarily unavailable", "detail": str(exc)},
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/events/ingest", response_model=IngestResponse)
async def ingest(payload: IngestRequest, db: AsyncSession = Depends(get_db)):
    """Batch ingest up to 500 events. Idempotent by event_id."""
    return await ingest_events(payload.events, db)


@app.get("/stores/{store_id}/metrics")
async def metrics(
    store_id: str = Path(
        ...,
        description="Valid store IDs: ST1008, ST1076",
        example="ST1008",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Real-time store metrics: unique visitors, conversion rate, dwell, queue, abandonment."""
    return await compute_metrics(store_id, db)

@app.get("/stores/{store_id}/funnel")
async def funnel(
    store_id: str = Path(
        ...,
        description="Valid store IDs: ST1008, ST1076",
        example="ST1008",
    ),
    db: AsyncSession = Depends(get_db),
):
    return await compute_funnel(store_id, db)

@app.get("/stores/{store_id}/heatmap")
async def heatmap(
    store_id: str = Path(
        ...,
        description="Valid store IDs: ST1008, ST1076",
        example="ST1008",
    ),
    db: AsyncSession = Depends(get_db),
):
    return await compute_heatmap(store_id, db)

@app.get("/stores/{store_id}/anomalies")
async def anomalies(
    store_id: str = Path(
        ...,
        description="Valid store IDs: ST1008, ST1076",
        example="ST1008",
    ),
    db: AsyncSession = Depends(get_db),
):
    return await compute_anomalies(store_id, db)

@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Service health: DB connectivity and per-camera feed staleness."""
    return await compute_health(db)