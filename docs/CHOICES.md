# CHOICES.md — Engineering Decisions

Three decisions made during this build, each documented as the challenge requires: options considered, what AI suggested, what was chosen, and why.

---

## Decision 1: Detection Model — YOLOv8n

### Problem

Person detection is the foundation of every downstream metric. The wrong model makes the pipeline either too slow to run on a CPU or too inaccurate to be useful. The challenge footage is 1080p at 15fps, and the submission constraint is a single-machine Docker deployment with no GPU assumed.

### Options Considered

| Model | Params | ~CPU ms/frame | Notes |
|---|---|---|---|
| YOLOv8n | 3.2M | 20–40ms | Auto-downloads via Ultralytics; lowest weight |
| YOLOv8s | 11M | 50–80ms | Better occlusion handling; 3–6× real-time on CPU |
| YOLOv8m | 25M | 120–200ms | Production accuracy; impractical on CPU for 20-min clips |
| RT-DETR | 32M | 300ms+ | Transformer architecture; strong accuracy; no viable CPU path |
| MediaPipe | ~3M | 10ms | Fast but designed for single-person / low-density scenes |


Performance figures are approximate values from published benchmarks and documentation; actual throughput depends on hardware and runtime environment.
### What AI Suggested

Claude suggested YOLOv8s. The argument was that it handles partial occlusion significantly better than nano, which is a documented failure mode in dense billing queue frames where people standing close together have overlapping bounding boxes. The suggestion was technically correct on accuracy grounds.

### Final Choice

**YOLOv8n**, loaded from `./models/yolov8n.pt` (path configurable via `YOLO_MODEL` env var), confidence threshold set to `0.25` (configurable via `YOLO_CONFIDENCE`).

### Why

At 15fps, YOLOv8s at 50–80ms per frame means 3–6× real-time on CPU. A 20-minute clip would take 60–120 minutes to process. YOLOv8n at 20–40ms processes a 20-minute clip in roughly 20–40 minutes — still slow, but within the window where it can complete before a submission deadline. The challenge states explicitly that low-confidence detections should be emitted rather than suppressed, and the `confidence` field in every event preserves uncertainty. A system that finishes processing all clips and feeds the API is more useful than a more accurate system that cannot process the full dataset.

The confidence threshold was lowered from the default 0.4 to 0.25 after observing that face-blurred footage reduces per-frame detection confidence on the head region. Lower threshold recovers some of those detections at the cost of more false positives, which the `is_staff` flag and `confidence` field in the event schema help downstream consumers handle.

### Trade-offs

YOLOv8n is more susceptible to missed detections under heavy occlusion than larger model variants, particularly in crowded billing-queue scenes where multiple customers overlap. This may lead to undercounting of visitors and affect downstream metrics such as conversion rate and queue depth. To mitigate this, the pipeline emits all detections that pass the configured confidence threshold and preserves the original confidence score in each event rather than discarding lower-confidence observations. Track fragmentation during prolonged occlusion remains the highest-risk failure point for queue-depth accuracy.

### Future Upgrade Path

`YOLO_MODEL=yolov8s.pt` in `.env` is the entire change needed when a GPU becomes available. The detection pipeline is model-agnostic — any Ultralytics-compatible model drops in without code changes.

---

## Decision 2: Event Schema Design Rationale

### Problem

The detection pipeline emits raw bounding boxes and track IDs. The API consumes structured business events. The schema connecting them needs to support eight distinct event types, POS correlation by time window, staff exclusion across all analytics queries, re-entry deduplication in the funnel, and queue depth tracking. It also needs to be the contract between two independently runnable systems.

### Options Considered

Three schema design questions had non-obvious answers:

**How to handle staff exclusion across all query paths:** Option A was a separate `staff_events` table. Option B was a single `events` table with an `is_staff` column filtered in every query. Option C was to drop staff events entirely.

**How to store metadata (queue_depth, sku_zone, session_seq):** Option A was nested JSON in a `metadata` column. Option B was flat columns on the events table. Option C was a separate `event_metadata` table joined at query time.

**How to represent POS correlation:** Option A was a direct foreign key from events to transactions (impossible — no customer identity in POS data). Option B was a separate `converted_visitors` table populated at ingest time. Option C was a time-window JOIN at query time: any visitor in a billing zone within a configurable window before a transaction counts as converted.

### What AI Suggested

For staff exclusion, the AI recommended a separate `staff_events` table with a view that unions both tables for total counts. The argument was clean separation of concerns and faster staff-only queries.

For metadata, the AI suggested a flat schema for queryability rather than nested JSON, correctly identifying that a `metadata TEXT` JSON column would require SQLite's `json_extract()` in every analytics query.

For POS correlation, the AI suggested implementing the challenge spec's exact 5-minute window as a JOIN, but flagged that the spec says "5 minutes before" while noting that the actual window used should be documented clearly.

### Final Choice

**Single `events` table with flat columns, `is_staff INTEGER` filter on every analytics query, time-window JOIN for POS correlation.**

The schema as implemented in `storage/schema.sql`:

```sql
CREATE TABLE events (
    event_id    TEXT PRIMARY KEY,
    store_id    TEXT NOT NULL,
    camera_id   TEXT NOT NULL,
    visitor_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    zone_id     TEXT,
    dwell_ms    INTEGER DEFAULT 0,
    is_staff    INTEGER DEFAULT 0,
    confidence  REAL NOT NULL,
    queue_depth INTEGER,
    sku_zone    TEXT,
    session_seq INTEGER DEFAULT 0,
    ingested_at TEXT NOT NULL
);
```

POS correlation query (used identically in `metrics.py`, `funnel.py`, and `anomalies.py`):

```sql
SELECT COUNT(DISTINCT e.visitor_id)
FROM events e
INNER JOIN pos_transactions p
    ON  p.store_id = e.store_id
    AND datetime(p.timestamp) >= datetime(e.timestamp)
    AND datetime(p.timestamp) <= datetime(e.timestamp, '+300 seconds')
WHERE e.store_id = :sid
  AND e.zone_id LIKE '%BILLING%'
  AND e.is_staff = 0
```

The window is 5 minutes (`+300 seconds`), matching the challenge specification. The specification defines a converted visitor as one who was present in the billing zone within the 5-minute window preceding a transaction. Because the POS dataset does not contain customer identifiers that can be linked directly to CCTV observations, conversion is inferred using a time-based correlation between billing-zone events and POS transactions. The JOIN therefore counts visitors who entered the billing area and were followed by a transaction within the specified 5-minute window.


### Why

The AI's recommendation against a separate staff table was overridden. A separate table would require UNION or view logic in every analytics query, and any query that forgot to apply the union would silently include staff metrics. The single-table approach with `AND is_staff = 0` in every WHERE clause is more failure-resistant: every query explicitly filters, and a missing filter is visually obvious in a code review.

Flat columns over nested JSON is the AI suggestion I agreed with completely. `queue_depth`, `sku_zone`, and `session_seq` all have concrete indexed or aggregated use cases. Storing them in a JSON column would require `json_extract()` in every query that touches them, which is slower and less readable.

The Pydantic `StoreEvent` model in `app/models.py` validates all eight event types as a `Literal` enum, validates UUID format on `event_id`, validates ISO-8601 format on `timestamp`, and enforces confidence range [0.0, 1.0] via `Field(ge=0.0, le=1.0)`. Ingest is idempotent: the `SELECT 1 FROM events WHERE event_id = :eid` check before every INSERT means the same batch can be POSTed multiple times without double-counting.

### Trade-offs

The LIKE `'%BILLING%'` pattern for zone matching requires that all billing-area zone IDs contain the string `BILLING`. This is enforced by convention in `store_layout.json` and is documented in the README. A more robust approach would be a `zone_type` column or a separate `billing_zones` config table, but that adds schema complexity without changing the metrics for the current dataset.

Multiple visitors in the billing zone simultaneously all match any transaction within the window. In a queued scenario with three customers and one paying, conversion count is overcounted by up to 3×. This is an acknowledged limitation of time-window correlation without customer identity.

---

## Decision 3: API Architecture — FastAPI + SQLite + JSONL Ingest Pipeline

### Problem

The API must validate incoming event payloads with a 12-field schema, compute five distinct analytics queries in real time, handle concurrent read requests, degrade gracefully when the database is unavailable, and start via `docker compose up` on any machine without manual setup steps.

### Options Considered

**Framework:**
- Flask (sync): Simple, widely known. No native async. Field validation is manual — a missing `confidence` field would not be caught until application code tried to read it.
- FastAPI (async): Pydantic validation built in. Async SQLAlchemy compatible. Auto-generated `/docs` page. Structured error responses by default via `RequestValidationError` handler.
- Django REST Framework: Appropriate for large team codebases with ORM-heavy data models. Heavyweight for six endpoints.

**Storage:**
- SQLite + aiosqlite: File-based, zero setup, no server process, works inside Docker volume.
- PostgreSQL: Production-grade. Concurrent writes. Per-store partitioning. Requires a third Docker service, a startup health check, and connection configuration.
- DuckDB: Excellent for aggregation-heavy analytical queries. Write-optimised for OLAP, not for mixed ingest + read workloads.

**Event transport (pipeline → API):**
- Direct DB writes from pipeline: Couples detection code to storage layer. No replay capability.
- Redis Streams: Decoupled, real-time capable. Requires a fourth Docker service.
- JSONL files + HTTP ingest: Pipeline writes one JSONL per camera. `ingest_events.py` POSTs batches to `/events/ingest`. Offline-capable, replayable, auditable on disk.

### What AI Suggested

The AI recommended PostgreSQL with a Docker service, arguing that SQLite's single-writer limitation would become a bottleneck under concurrent ingest. It also suggested Redis Streams for event transport as a common production pattern that decouples the pipeline from the API.

Both suggestions are correct for a production system at scale. Both were rejected for this implementation.

### Final Choice

**FastAPI + aiosqlite (async SQLite driver) + JSONL files with HTTP ingest.**

Two Docker services: `api` (FastAPI + uvicorn) and `dashboard` (Streamlit). The `api` service exposes all six endpoints. The `dashboard` service polls all endpoints every 5 seconds via `st.rerun()` and displays metrics, funnel stages, heatmap table, anomaly cards, and system health for both configured store IDs (`ST1076`, `ST1008`).

API endpoints as implemented:

| Endpoint | Method | What it returns |
|---|---|---|
| `/events/ingest` | POST | `{accepted, rejected, duplicates, errors[]}` — idempotent by `event_id`, max 500 events per batch |
| `/stores/{id}/metrics` | GET | `{unique_visitors, conversion_rate, avg_dwell_per_zone[], queue_depth, abandonment_rate, total_transactions, computed_at}` |
| `/stores/{id}/funnel` | GET | `{stages: [{stage, count, drop_off_pct}]}` — 4 stages: Entry → Zone Visit → Billing Queue → Purchase |
| `/stores/{id}/heatmap` | GET | Per-zone visit frequency and avg dwell normalised 0–100, with `data_confidence` flag when session count < 20 |
| `/stores/{id}/anomalies` | GET | `{anomalies: [{anomaly_type, severity, description, suggested_action, detected_at}]}` |
| `/health` | GET | `{status, db_connected, last_event_per_store{}, stale_feed, checked_at}` |

Anomaly detection uses static thresholds rather than a 7-day rolling baseline. The challenge spec references "conversion drop vs 7-day avg" but there is no 7-day historical data available — only the current session's clips. The implemented thresholds: queue WARN at depth ≥ 3, CRITICAL at depth ≥ 6; conversion WARN when rate < 10% (triggered only when total visitors > 10 to avoid noise on empty stores); DEAD_ZONE INFO when a zone has no `ZONE_ENTER` events in the last 30 minutes relative to the latest event timestamp in the store's data.

Health endpoint returns `stale_feed: true` when the most recent event timestamp for any store is more than 10 minutes behind wall-clock time, matching the challenge spec.

### Why

FastAPI was chosen over Flask primarily because of Pydantic validation. The `StoreEvent` model validates all twelve fields before any application code runs. Invalid events are rejected with structured error responses (`422 Unprocessable Entity` with field-level error details) rather than causing silent data corruption. The AI agreed with this choice.

SQLite was chosen over PostgreSQL because the challenge's acceptance gate is `docker compose up` on a clean machine with no manual setup. PostgreSQL adds a service that requires a health check before the API can start, connection string configuration, and a first-run initialisation step. For a single-store, batch-ingest workload — one pipeline run writes events, then the API serves reads — SQLite's single-writer limitation is not a bottleneck. The `aiosqlite` async driver means all five analytics endpoints handle concurrent read requests without blocking. The AI's PostgreSQL suggestion was overridden deliberately, with the reasoning documented here.

JSONL + HTTP ingest was chosen over direct DB writes and Redis Streams because it satisfies three requirements simultaneously: the pipeline can run offline without the API running, the JSONL files are auditable on disk for debugging, and `event_id` UUID deduplication in `ingest_events.py` makes the ingest idempotent. The same JSONL file can be posted multiple times during development without inflating counts. Redis Streams would require an additional Docker service and adds a dependency that can fail independently — not acceptable for an acceptance-gate requirement of `docker compose up`.

### Trade-offs

SQLite breaks at 40 concurrent stores with parallel ingest. The `DATABASE_URL` environment variable is the only change required to switch to PostgreSQL — the SQLAlchemy async layer is identical for `aiosqlite` and `asyncpg`. No application code changes are needed.

5-second Streamlit polling is not WebSocket push — there is up to a 5-second lag on metric updates. For a store intelligence dashboard where the primary consumers are operations managers reviewing shift-level trends, sub-second refresh is not a requirement. The dashboard is connected to all five API endpoints simultaneously and shows live anomaly cards, which is the highest-value real-time signal.

The JSONL ingest pipeline is batch, not streaming. Queue depth in the API reflects end-of-video state for pre-recorded clips. For live camera streams, the pipeline would switch from `cv2.VideoCapture(file_path)` to `cv2.VideoCapture(rtsp_url)` and replace `CLIP_START_UTC` with `datetime.now()` minus frame offset — one-line changes in `detect.py`. The rest of the architecture is unchanged.