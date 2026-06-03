# DESIGN.md — Store Intelligence System

## 1. System Overview

### Business Problem

Apex Retail operates physical stores with no offline analytics. Store managers know end-of-day transaction counts from the POS system but cannot answer:

- How many customers visited today?
- What fraction of visitors made a purchase?
- Which product zones attracted the most engagement?
- How long did customers wait at billing?
- Are customers abandoning the queue?

This system closes that gap by processing raw CCTV footage and correlating it with POS transaction data to produce the same category of analytics that online teams take for granted.

### Primary Metric

```
Offline Conversion Rate = Visitors who completed a purchase ÷ Total unique visitors
```

Every component either improves the accuracy of this number (detection layer) or makes it actionable (API layer).

### Stores Covered

Two stores are processed in this submission:

- **ST1076** — Purplle Store Mumbai 1076, footage from March 2026 (4 cameras: CAM3 entry, CAM1 zone, CAM2 zone, CAM6 billing)
- **ST1008** — second store, footage from April 2026 (4 cameras: CAM\_ENTRY\_1, CAM\_ENTRY\_2, CAM\_ZONE, CAM\_BILLING)

---

## 2. Architecture Overview

The system is a four-stage linear pipeline running on a single machine. There are no microservices, no message queues, and no distributed components. This is a deliberate choice: the challenge requires local deployment via `docker compose up`, and a working simple system scores higher than a complex non-functional one.

```
CCTV Videos (MP4, 1080p@15fps)
         │
         ▼  pipeline/detect.py  (one invocation per camera via run.sh)
   YOLOv8n inference  →  supervision ByteTrack  →  ReIDTracker  →  StaffDetector
         │
         ▼  pipeline/emit.py
   EventEmitter  →  <camera_id>_events.jsonl  →  merge_event_files()  →  all_events.jsonl
         │
         ▼  pipeline/ingest_events.py  (batch POST, up to 500 events per request)
   POST /events/ingest  →  Pydantic validation  →  event_id dedup  →  SQLite INSERT
         │
   ┌─────────────┐
   │   SQLite    │  ← pos_transactions loaded at API startup from pos_transactions.csv
   └─────────────┘
         │
         ▼  FastAPI (app/)
   /metrics  /funnel  /heatmap  /anomalies  /health
         │
         ▼  HTTP GET every 5 seconds
   Streamlit dashboard (dashboard/streamlit_app.py)  →  http://localhost:8501
```

### Component Summary

| Component | File(s) | Responsibility |
|---|---|---|
| Detection | `pipeline/detect.py` | YOLOv8n inference, line crossing, zone hit-testing per frame |
| Tracking | `pipeline/tracker.py` | ByteTrack wrapper (`CameraTracker`), visitor ID assignment (`ReIDTracker`), staff detection (`StaffDetector`) |
| Emission | `pipeline/emit.py` | Event construction (`build_event`), JSONL buffering (`EventEmitter`), multi-camera merge |
| Ingestion | `pipeline/ingest_events.py` | Reads `all_events.jsonl`, POSTs batches of 500 to the API |
| API entrypoint | `app/main.py` | FastAPI app, lifespan (schema apply + POS CSV load), middleware, routes |
| Pydantic models | `app/models.py` | `StoreEvent`, `IngestRequest`, all response models with validators |
| Ingest logic | `app/ingestion.py` | Dedup by `event_id`, INSERT, POS CSV parser |
| Metrics | `app/metrics.py` | Unique visitors, conversion rate, avg dwell, queue depth, abandonment rate |
| Funnel | `app/funnel.py` | 4-stage funnel with per-stage drop-off percentages |
| Heatmap | `app/heatmap.py` | Zone visit frequency + avg dwell, normalised 0–100 |
| Anomalies | `app/anomalies.py` | Queue spike, conversion drop, dead zone detection |
| Health | `app/health.py` | DB connectivity + per-camera feed staleness |
| Dashboard | `dashboard/streamlit_app.py` | Streamlit UI polling all endpoints every 5 seconds |
| Schema | `storage/schema.sql` | SQLite DDL for `events` and `pos_transactions` tables |
| Layout config | `config/store_layout.json` | Zone polygons, camera roles, entry line position, HSV staff parameters per store |

---

## 3. Component Breakdown

### 3.1 Detection Pipeline (`pipeline/detect.py`)

Invoked once per camera via `run.sh` with `--store`, `--camera`, `--video`, and `--clip-start` arguments. Opens the MP4 with OpenCV and runs YOLOv8n on every frame.

**YOLO invocation:**

```python
results = model(frame, conf=YOLO_CONF, classes=[0], verbose=False)[0]
detections = sv.Detections.from_ultralytics(results)
```

Key parameters:
- `classes=[0]` — COCO class 0 is "person". All non-person detections are suppressed before NMS.
- `YOLO_CONF=0.25` — set lower than the default (0.4) because face-blur applied to footage reduces discriminative features. Low-confidence detections that pass this threshold are emitted with their actual confidence score rather than being dropped, as required by the spec.
- `verbose=False` — suppresses per-frame stdout at 15fps.

**Entry/exit line crossing (entry cameras only):**

Each entry camera has an `entry_line_y` pixel value in `store_layout.json`. Direction is determined by centroid y-coordinate transition across that line:

```python
crossed_inbound  = prev_y < entry_line_y <= cy   # ENTRY or REENTRY
crossed_outbound = prev_y >= entry_line_y > cy   # EXIT
```

**Zone polygon hit-testing (zone and billing cameras):**

Zone membership uses `cv2.pointPolygonTest` against polygon coordinates from `store_layout.json`. When a track's centroid moves from one zone to another, the pipeline emits a `ZONE_EXIT` for the old zone and a `ZONE_ENTER` (or `BILLING_QUEUE_JOIN`) for the new one. `ZONE_DWELL` is emitted every 30 seconds of continuous presence in the same zone.

**Video validation:** The pipeline validates that the video file exists before attempting to open it. If the file is missing, it writes an empty JSONL and continues rather than crashing. This prevents `merge_event_files()` from failing when a camera's clip is absent.

### 3.2 Tracking (`pipeline/tracker.py`)

Three classes are defined here:

**`CameraTracker`** wraps `supervision.ByteTrack` with a 3-second lost-track buffer (`lost_track_buffer = int(fps * 3)`). This tolerates brief occlusions behind displays. It exposes only `update()` and `centroid()`; state management for per-track events lives in `detect.py`.

**`ReIDTracker`** manages the mapping from ByteTrack integer `track_id` → stable `VIS_NNNN` visitor ID string. When a new track appears:

1. The tracker checks the `_exited` pool for any visitor whose last-known centroid is within 200px and whose exit time is within 300 seconds (5 minutes).
2. If matched: existing `visitor_id` is reused and `is_reentry=True` is returned, triggering a `REENTRY` event in `detect.py`.
3. If not matched: a new sequentially numbered ID (`VIS_0001`, `VIS_0002`, ...) is assigned.

Exit records are evicted from the pool once they fall outside the 5-minute window. `get_last_centroid()` returns `None` for unknown tracks (guards against a crash in the vanished-track handler in `detect.py`).

**`StaffDetector`** runs an HSV uniform colour check per bounding box. HSV bounds are loaded from `store_layout.json` per store:

- ST1076: `lower=[160, 100, 100]`, `upper=[175, 255, 255]` (pink/magenta uniform)
- A person is classified as staff if more than 25% of the bbox pixels fall within the HSV range.

This applies to every camera. There is no camera-restriction on staff detection — it runs universally across all roles.

### 3.3 Event Emission (`pipeline/emit.py`)

`build_event()` constructs a dict matching the challenge schema exactly, including the nested `metadata` object. Timestamps are derived from frame index:

```python
offset_seconds = frame_idx / fps
ts = clip_start_utc + timedelta(seconds=offset_seconds)
```

`EventEmitter` buffers events in memory and writes a per-camera JSONL on `flush()`. `merge_event_files()` reads all `*_events.jsonl` files from `EVENTS_DIR`, sorts them chronologically (ISO-8601 lexicographic sort), and writes `all_events.jsonl`. The merge explicitly skips the output file itself to avoid self-inclusion on repeated runs.

### 3.4 API (`app/`)

**FastAPI** with async SQLAlchemy (`aiosqlite` driver). The lifespan handler applies `schema.sql` at startup and loads the POS CSV via `load_pos_transactions()`. Both operations are idempotent.

**Request logging middleware** logs every request with: `trace_id` (8-char UUID prefix), `store_id` (from path params), `endpoint`, `latency_ms`, `status_code`. Structured as JSON lines.

**Error handling:** Pydantic `RequestValidationError` → 422 with structured detail. All unhandled exceptions → 503 with `{"error": "Service temporarily unavailable", "detail": "..."}`. No raw stack traces are exposed in responses.

---

## 4. API Endpoints

### `POST /events/ingest`

Accepts a JSON body `{"events": [...]}` with up to 500 `StoreEvent` objects (enforced by Pydantic validator). For each event:
- Checks for existing `event_id` in the database — duplicates are counted separately and skipped.
- Inserts accepted events; any per-event exception increments `rejected` and appends an error message.
- Returns `{"accepted": N, "rejected": M, "duplicates": K, "errors": [...]}`.

Idempotent by `event_id`. Partial success — a malformed event does not abort the batch.

### `GET /stores/{store_id}/metrics`

Returns:
- `unique_visitors`: `COUNT(DISTINCT visitor_id)` where `is_staff = 0`
- `conversion_rate`: converted visitors ÷ unique visitors (0.0 if zero visitors)
- `avg_dwell_per_zone`: list of `{zone_id, avg_dwell_seconds, visit_count}` from `ZONE_DWELL` and `ZONE_ENTER` events
- `queue_depth`: visitors with `BILLING_QUEUE_JOIN` who have not subsequently had `EXIT` or `BILLING_QUEUE_ABANDON`
- `abandonment_rate`: abandoned ÷ joined (visitor-level, not event-level)
- `total_transactions`: count of rows in `pos_transactions` for this store

Conversion is computed via an INNER JOIN between billing-zone events and `pos_transactions` on the same store, where the transaction timestamp falls within 300 seconds (5 minutes) after the billing event timestamp. This follows the challenge specification, which defines a converted visitor as one who was present in the billing zone within the 5-minute window preceding a transaction. Because the provided dataset does not contain customer identifiers linking CCTV observations to POS transactions, conversion is inferred using this time-based correlation heuristic.

### `GET /stores/{store_id}/funnel`

Four stages, each using `COUNT(DISTINCT visitor_id)` so re-entries do not double-count:
1. **Entry** — `ENTRY` events, `is_staff = 0`
2. **Zone Visit** — `ZONE_ENTER` events where `zone_id NOT LIKE '%BILLING%'`
3. **Billing Queue** — `BILLING_QUEUE_JOIN` events
4. **Purchase** — same billing-zone + POS correlation query as `/metrics`

Drop-off percentage per stage = `(previous_count - current_count) / previous_count × 100`.

### `GET /stores/{store_id}/heatmap`

Per zone: `visit_frequency` (`COUNT(DISTINCT visitor_id)` from `ZONE_ENTER` and `ZONE_DWELL`), `avg_dwell_seconds`, `normalised_score` (0–100, scaled to the max-frequency zone), `data_confidence` (True if ≥ 20 unique visitors, False otherwise). Results are sorted descending by `normalised_score`.

### `GET /stores/{store_id}/anomalies`

Three anomaly types:
- **BILLING\_QUEUE\_SPIKE** — WARN if current queue depth ≥ 3, CRITICAL if ≥ 6. Queue depth computed the same way as `/metrics`.
- **CONVERSION\_DROP** — WARN if conversion rate < 10% and total visitors > 10 (minimum data threshold before flagging).
- **DEAD\_ZONE** — INFO per zone that has had no `ZONE_ENTER` events (customer, non-billing) in the 30 minutes relative to the most recent event timestamp in the database. Uses relative time rather than wall-clock time so it works correctly against batch-processed historical clips.

Each anomaly includes `anomaly_type`, `severity`, `description`, `suggested_action`, and `detected_at`.

### `GET /health`

Attempts `SELECT 1` against the database. If that fails, returns `{"status": "down", "db_connected": false, "stale_feed": true}` with an empty `store_feeds` list.

On success, queries `MAX(timestamp)` per `camera_id`. A camera is marked `stale=true` if its last event is more than 10 minutes behind wall clock. Overall status is `"ok"` if no cameras are stale, `"degraded"` if any are. Also returns the global last event timestamp.

---

## 5. Database Design

SQLite, accessed via async SQLAlchemy (`aiosqlite`). Two tables.

### `events`

```sql
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    store_id     TEXT NOT NULL,
    camera_id    TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    timestamp    TEXT NOT NULL,       -- ISO-8601 UTC
    zone_id      TEXT,
    dwell_ms     INTEGER DEFAULT 0,
    is_staff     INTEGER DEFAULT 0,   -- 0=false 1=true (SQLite has no BOOLEAN)
    confidence   REAL NOT NULL,
    queue_depth  INTEGER,
    sku_zone     TEXT,
    session_seq  INTEGER DEFAULT 0,
    ingested_at  TEXT NOT NULL        -- wall-clock time of API ingest
);
```

The `metadata` fields from the event schema (`queue_depth`, `sku_zone`, `session_seq`) are flattened into top-level columns for SQL query efficiency. The Pydantic `StoreEvent` model preserves the nested `metadata` structure for API input/output.

### `pos_transactions`

```sql
CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id  TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,    -- ISO-8601 UTC (converted from IST)
    basket_value    REAL NOT NULL
);
```

The POS CSV has one row per SKU (`order_id`, `order_date`, `order_time`, `store_id`, `product_id`, `brand_name`, `total_amount`). `load_pos_transactions()` groups rows by a composite key of `store_id + order_date + order_time` (or by `invoice_number` if present) and sums `total_amount` per order. Dates in `DD-MM-YYYY` format are parsed with `ZoneInfo("Asia/Kolkata")` and converted to UTC before storage.

### Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_events_store_ts` | `(store_id, timestamp)` | POS correlation JOIN, dead zone window |
| `idx_events_visitor` | `(visitor_id)` | Session reconstruction, funnel dedup |
| `idx_events_type` | `(event_type)` | Funnel stage counts |
| `idx_events_store_type` | `(store_id, event_type)` | Unique visitor count, queue depth |
| `idx_events_zone` | `(zone_id)` | Heatmap, dead zone |
| `idx_pos_store_ts` | `(store_id, timestamp)` | POS correlation JOIN |

### Design Notes

- Timestamps stored as ISO-8601 text strings. SQLite's `datetime()` function enables arithmetic: `datetime(e.timestamp, '+300 seconds')`.
- `is_staff` stored as INTEGER because SQLite has no BOOLEAN. All customer-filtering queries use `is_staff = 0`.
- `ingested_at` records wall-clock insertion time, separate from event `timestamp`, for ingest latency debugging.

---

## 6. Dashboard

`dashboard/streamlit_app.py` is a Streamlit app that runs in its own Docker container (`store_intelligence_dashboard` on port 8501). It polls all API endpoints every 5 seconds using a `while True` loop with `st.empty()` placeholder replacement.

**Per store displayed:**
- Four metric tiles: unique visitors, conversion rate, queue depth, abandonment rate
- Conversion funnel: one tile per stage with drop-off delta
- Zone heatmap: normalised score tiles with dwell and confidence tooltip
- Anomalies: each rendered as a coloured expander block (red = CRITICAL, orange = WARN, blue = INFO)
- Per-camera feed status from `/health`

The dashboard connects to the API via the `API_URL` environment variable, which is set to `http://api:8000` inside Docker (service-name DNS resolution) and falls back to `http://localhost:8000` for local runs. Store IDs are read from the `STORE_IDS` environment variable (default: `ST1076,ST1008`).

---

## 7. Testing

Nine test files covering the pipeline, API logic, and HTTP layer:

| File | Coverage |
|---|---|
| `test_pipeline.py` | StoreEvent model (all 8 event types), IngestRequest batch limit, EventEmitter buffer/flush, `build_event` timestamp arithmetic, `ReIDTracker` (new ID, same track, re-entry, no-match, centroid update), `merge_event_files` (sort, self-skip) |
| `test_ingestion.py` | Ingest dedup by event_id, partial batch failure, POS CSV load |
| `test_metrics.py` | Zero-visitor store, staff exclusion, queue depth, abandonment rate, conversion formula, avg dwell, total_transactions |
| `test_funnel.py` | Stage counts, drop-off percentages, re-entry deduplication |
| `test_heatmap.py` | Normalisation, data_confidence threshold (< 20 sessions), empty store |
| `test_anomalies.py` | Queue spike WARN/CRITICAL thresholds, conversion drop trigger, dead zone detection |
| `test_health.py` | DB connectivity, stale feed detection, degraded status |
| `test_ingest_events.py` | HTTP-layer ingest via FastAPI test client |
| `test_main.py` | HTTP-layer smoke tests for all endpoints |

Tests use an in-memory SQLite database (`sqlite+aiosqlite://` with `StaticPool`). The real `schema.sql` is applied before each test via `conftest.py`. This tests actual SQL queries — not mocks — so column name errors and missing `is_staff = 0` filters are caught. The test DB engine and session factory are patched into `app.main` before the application is imported, using `main_mod.engine` and `main_mod.SessionLocal` monkeypatching.

---

## 8. Assumptions

| Assumption | Where It Applies | Production Requirement |
|---|---|---|
| Clip start time is known and passed via `--clip-start` | `detect.py` | Embedded camera timestamps or NTP sync |
| Video files are pre-recorded MP4s, not RTSP streams | `cv2.VideoCapture(file_path)` | Replace path with RTSP URL; remove `--clip-start` |
| Zone polygons estimated from blueprint images | `store_layout.json` | Manual calibration overlay on real camera frames |
| POS CSV timestamps are in IST (`Asia/Kolkata`) | `ingestion.py` ZoneInfo | Configurable timezone per store |
| Staff uniform colour is known per store | `store_layout.json` HSV bounds | HSV range calibration from sample frames |
| CPU-only inference is acceptable | `yolov8n.pt`, no CUDA | GPU enables larger models and OSNet Re-ID |
| Batch processing is acceptable for Part A/B | JSONL intermediate, then single ingest run | RTSP streaming for live queue metrics |
| Historical clips may start while customers are already inside | Zone counts can exceed entry counts | Clips anchored to store open time |

---

## 9. Limitations

### 9.1 Detection Accuracy (YOLOv8n)

YOLOv8n (3.2M parameters) trades accuracy for CPU inference speed. Known failure modes:
- Persons fully occluded behind displays for more than 3 seconds receive new track IDs after ByteTrack's buffer expires, potentially producing spurious `ZONE_ENTER` events.
- Dense bounding box overlap (group entry) may trigger NMS suppression, causing undercounting. The pipeline emits individual detections; count accuracy in crowded scenes depends on NMS threshold.
- Face blur applied to footage reduces person-detection confidence, which is why `YOLO_CONF` is set to 0.25 rather than the default 0.4.

**Business impact:** Unique visitor counts may be lower than ground truth in crowded scenarios due to missed detections and occlusions, which can overestimate conversion rate.

### 9.2 Centroid Re-ID False Matches

Two different customers entering from similar positions within 5 minutes produce a false `REENTRY` event and are merged into a single session. In practice, for a single-door store with one fixed entry camera, the 200px threshold is reasonably selective, but this is not a production-grade approach.

### 9.3 POS Correlation Over-counts Conversion

Multiple visitors simultaneously in the billing zone all match any POS transaction within the correlation window. In a busy queue with three customers, one transaction counts all three as converted. This is acknowledged but accepted — the challenge spec defines the correlation method and this implementation follows it exactly.

### 9.4 Zone Polygon Accuracy

Zone coordinates were estimated from blueprint/layout images. Centroid-to-polygon assignment for visitors near zone edges may be inaccurate near zone boundaries.

### 9.5 Queue Depth is a Batch Metric

After batch ingest, `queue_depth` reflects the state at end of clip processing, not a live real-time value. The live dashboard polls this number every 5 seconds, but it does not change between pipeline runs.

### 9.6 SQLite Concurrency

SQLite allows one writer at a time. The current architecture has a single ingest process posting sequentially. Parallel multi-store concurrent ingest would require PostgreSQL.

---

## 10. Scalability Discussion

### Current Limits

Single machine, batch MP4 processing, sequential camera processing via `run.sh`, one SQLite file, one API process.

### Multiple Stores at Scale (40 stores)

- Migrate to PostgreSQL with per-store partitioning on `store_id`.
- Replace `cv2.VideoCapture(file)` with RTSP URLs — code change is one line in `detect.py`.
- Job scheduler (Celery or Airflow) for parallel per-store detection pipelines.
- API connection pooling (PgBouncer) and horizontal API scaling behind a load balancer.

### Real-Time Streaming

- Replace `--clip-start` anchor with `datetime.now(timezone.utc)` minus frame offset.
- Replace `EventEmitter` JSONL buffering with a Redis Stream or Kafka producer.
- Replace Streamlit 5-second polling with WebSocket subscriptions.

### GPU Deployment

- Set `YOLO_MODEL=yolov8s.pt` or `yolov8m.pt` in `.env` — no code changes needed.
- Add `device='cuda'` to the YOLO constructor.
- Add OSNet Re-ID via torchreid to `ReIDTracker` — the `get_visitor_id()` interface is unchanged; swap the internals.

### First Bottleneck at 40 Stores

SQLite write lock — resolved by migrating to PostgreSQL. Second bottleneck: the POS correlation JOIN in `/metrics` and `/funnel` is `O(billing_events × pos_transactions)` — a partial index on `(store_id, timestamp) WHERE zone_id LIKE '%BILLING%'` would reduce it to a small billing-zone subset.

---

## 11. AI-Assisted Decisions

### 11.1 Zone Polygon Coordinates

**AI contribution:** Estimated pixel coordinates from blueprint images, scaling millimeter dimensions proportionally to camera resolution.

**Accepted:** Starting polygon coordinates for all zones in `store_layout.json`.

**Not done:** Production calibration against real camera frames. The coordinates are estimated from layout images and may not perfectly align with real camera views.

### 11.2 POS Correlation Window

**AI suggested:** Add a minimum 30-second dwell requirement in the billing zone before counting a visitor as converted, to reduce false matches from walk-through visitors.

**Rejected:** The challenge spec defines the correlation method as "billing-zone event within 5 minutes before a transaction." Adding an undocumented filter changes the metric definition. The spec was followed exactly, and the known overcount risk is documented in §9.3 above.

### 11.3 Re-ID Strategy

**AI suggested:** OSNet via torchreid for appearance-based Re-ID — correctly identified as the production standard. Provided a working sketch using `torchreid.utils.FeatureExtractor`.

**Rejected:** Appearance-based Re-ID models such as OSNet were significantly slower than the lightweight centroid-based approach on CPU-only hardware, making them impractical for this challenge's processing constraints. Centroid proximity (`dist < 200px` within 300 seconds) was chosen as a viable CPU alternative. The OSNet approach is documented as the correct production upgrade path in CHOICES.md.

### 11.4 Dead Zone Anomaly Clock Reference

**AI suggested:** Use `datetime.now(timezone.utc)` as the reference time for the dead zone 30-minute window.

**Rejected:** Against batch-processed historical clips, `now()` would flag every zone as dead immediately. Instead, the implementation uses the most recent event timestamp in the database as the reference point (`MAX(timestamp)` from `events`), which correctly identifies zones with no activity in the last 30 minutes of clip time. This was an override based on reasoning about what the anomaly should actually mean in batch-replay mode.
