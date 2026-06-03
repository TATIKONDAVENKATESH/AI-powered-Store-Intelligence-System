# Store Intelligence System

> CCTV footage → person detection → structured events → live analytics API → dashboard.

**North Star Metric:** `Conversion Rate = Purchasing Visitors ÷ Total Unique Visitors`

---

## Architecture

```
MP4 clips → detect.py (YOLOv8n) → tracker.py (ByteTrack) → emit.py (JSONL)
    → POST /events/ingest → SQLite ← pos_transactions.csv
    → GET /metrics /funnel /heatmap /anomalies /health
    → Streamlit dashboard (5s refresh)
```

Single machine. No message queues. Starts with `docker compose up`.

---

## Two Stores

| Store | Footage | Cameras |
|---|---|---|
| `ST1076` | March 2026 | CAM3 (entry), CAM1 (zone), CAM2 (zone), CAM6 (billing) |
| `ST1008` | April 2026 | CAM_ENTRY_1 (entry), CAM_ENTRY_2 (entry), CAM_ZONE (zone), CAM_BILLING (billing) |

---

## Quick Start

### Option A — No videos needed (pre-generated events)

```bash
git clone <repo-url> store-intelligence && cd store-intelligence
docker compose up --build

# new terminal
bash pipeline/run_sample.sh      # Linux/macOS
pipeline\run_sample.bat          # Windows

Dashboard: http://localhost:8501
API Docs:  http://localhost:8000/docs
```

### Option B — Full pipeline (videos required)

```bash
docker compose up --build

bash pipeline/run.sh             # Linux/macOS
pipeline\run.bat                 # Windows
```

### Local (no Docker)

```bash
pip install torch==2.3.0+cpu torchvision==0.18.0+cpu \
    --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000   # terminal 1
bash pipeline/run_sample.sh                         # terminal 2
streamlit run dashboard/streamlit_app.py            # terminal 3
```

---

## Dataset

Videos are **not included**. Place MP4 files here before running the full pipeline:

```
data/Videos/
├── Store 1/
│   ├── CAM 1 - zone.mp4
│   ├── CAM 2 - zone.mp4
│   ├── CAM 3 - entry.mp4
│   └── CAM 5 - billing.mp4
└── Store 2/
    ├── entry 1.mp4
    ├── entry 2.mp4
    ├── zone.mp4
    └── billing_area.mp4
```

**Included in repo:**
- `data/pos_transactions.csv` — POS data (columns: `order_id, order_date, order_time, store_id, product_id, brand_name, total_amount`)
- `data/generated_events/all_events.jsonl` — 1,479 pre-generated events from both stores
- `data/generated_events/sample_events.jsonl` — 13 events for quick testing

The sample event file allows viewers to test the API and dashboard
without running YOLO inference or providing CCTV footage.

- `config/store_layout.json` — zone polygons, camera roles, staff HSV params

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/ingest` | Batch ingest ≤500 events. Idempotent by `event_id`. |
| `GET` | `/stores/{id}/metrics` | Visitors, conversion rate, dwell, queue depth, abandonment |
| `GET` | `/stores/{id}/funnel` | Entry → Zone → Billing Queue → Purchase with drop-off % |
| `GET` | `/stores/{id}/heatmap` | Zone visit frequency + avg dwell, normalised 0–100 |
| `GET` | `/stores/{id}/anomalies` | Queue spike, conversion drop, dead zone |
| `GET` | `/health` | DB status + per-camera feed staleness |

```bash
curl http://localhost:8000/stores/ST1076/metrics
curl http://localhost:8000/stores/ST1008/funnel
curl http://localhost:8000/health
```

---

## Implementation Notes

- **Conversion window:** 5-minute billing-zone × POS correlation (matches challenge specification; visitors in billing within 300 seconds before a transaction are counted as converted)
- **Dead zone:** computed relative to `MAX(timestamp)` in the dataset, not wall-clock time (prevents all historical clips from appearing stale)
- **YOLO confidence:** 0.25 (lowered from the default 0.4 because face-blurred footage reduces detection confidence)
- **Staff detection:** HSV uniform-colour classification per store (pink/magenta for ST1076, black for ST1008)
- **POS grouping:** CSV contains product-level rows; grouped by `order_id` into a single transaction representing one customer basket
---

## Testing

```bash
pytest tests/ -v --cov=app --cov=pipeline --cov-report=term-missing
```

10 test files, all using in-memory SQLite. Each file has a `# PROMPT:` / `# CHANGES MADE:` block at the top.

---

## Known Limitations

- Clips may begin after customers are already inside — zone visitors can exceed entry counts
- No cross-camera Re-ID — same person on two cameras gets two `visitor_id` values
- Dwell undercounts in crowded scenes due to tracking drops under occlusion
- POS correlation overestimates in busy periods (multiple billing visitors matched to one transaction)
- Queue depth reflects end-of-clip state, not a live value

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./storage/store_intelligence.db` | DB connection |
| `POS_CSV` | `./data/pos_transactions.csv` | POS input file |
| `LAYOUT_JSON` | `./config/store_layout.json` | Zone + camera config |
| `YOLO_MODEL` | `yolov8n.pt` | YOLO weights |
| `YOLO_CONFIDENCE` | `0.25` | Detection threshold |
| `EVENTS_DIR` | `./data/generated_events` | JSONL output dir |
| `API_URL` | `http://localhost:8000` | Used by ingest scripts |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Repository Structure

```
store-intelligence/
├── pipeline/       detect.py  tracker.py  emit.py  ingest_events.py  run.sh  run.bat  run_sample.sh  run_sample.bat
├── app/            main.py  models.py  ingestion.py  metrics.py  funnel.py  heatmap.py  anomalies.py  health.py
├── storage/        schema.sql
├── data/           Videos/  pos_transactions.csv  generated_events/
├── config/         store_layout.json
├── tests/          8 test files + conftest.py
├── docs/           DESIGN.md  CHOICES.md
├── dashboard/      streamlit_app.py
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```