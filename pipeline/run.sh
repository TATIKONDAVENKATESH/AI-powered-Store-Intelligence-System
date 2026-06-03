#!/usr/bin/env bash
# ============================================================
#  Store Intelligence Detection Pipeline (Linux / macOS / WSL)
#  Usage: bash pipeline/run.sh
#  Prerequisites:
#    1. docker compose up  (API must be running before ingest)
#    2. Place MP4 files in data/videos/ with exact filenames
#    3. Place POS CSV at data/pos_transactions.csv
# ============================================================

set -e   # exit on first error

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

export EVENTS_DIR="${EVENTS_DIR:-./data/generated_events}"
export API_URL="${API_URL:-http://localhost:8000}"
mkdir -p "$EVENTS_DIR"

echo "=== Store Intelligence Detection Pipeline ==="
echo "Project root: $PROJECT_ROOT"
echo "Events dir:   $EVENTS_DIR"
echo ""

# ── STORE 1: ST1076 (March 2026 footage) ─────────────────────────────────────

echo "--- ST1076: Processing entry camera (CAM3) ---"
python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM3 \
    --video  "data/videos/Store 1/CAM 3 - entry.mp4" \
    --clip-start "2026-03-08T13:00:00" || echo "[WARN] CAM3 failed or video missing"

echo "--- ST1076: Processing zone camera A (CAM1) ---"
python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM1 \
    --video  "data/videos/Store 1/CAM 1 - zone.mp4" \
    --clip-start "2026-03-08T13:00:00" || echo "[WARN] CAM1 failed or video missing"

echo "--- ST1076: Processing zone camera B (CAM2) ---"
python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM2 \
    --video  "data/videos/Store 1/CAM 2 - zone.mp4" \
    --clip-start "2026-03-08T13:00:00" || echo "[WARN] CAM2 failed or video missing"

echo "--- ST1076: Processing billing camera (CAM6) ---"
python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM6 \
    --video  "data/videos/Store 1/CAM 5 - billing.mp4" \
    --clip-start "2026-03-08T13:00:00" || echo "[WARN] CAM6 failed or video missing"

# ── STORE 2: ST1008 (April 2026 footage) ─────────────────────────────────────

echo "--- ST1008: Processing entry camera 1 ---"
python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_ENTRY_1 \
    --video  "data/videos/Store 2/entry 1.mp4" \
    --clip-start "2026-04-10T06:30:00" || echo "[WARN] CAM_ENTRY_1 failed or video missing"

echo "--- ST1008: Processing entry camera 2 ---"
python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_ENTRY_2 \
    --video  "data/videos/Store 2/entry 2.mp4" \
    --clip-start "2026-04-10T06:30:00" || echo "[WARN] CAM_ENTRY_2 failed or video missing"

echo "--- ST1008: Processing zone camera ---"
python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_ZONE \
    --video  "data/videos/Store 2/zone.mp4" \
    --clip-start "2026-04-10T06:30:00" || echo "[WARN] CAM_ZONE failed or video missing"

echo "--- ST1008: Processing billing area camera ---"
python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_BILLING \
    --video  "data/videos/Store 2/billing_area.mp4" \
    --clip-start "2026-04-10T06:30:00" || echo "[WARN] CAM_BILLING failed or video missing"

# ── Merge all per-camera JSONL files into all_events.jsonl ───────────────────

echo ""
echo "--- Merging all event files ---"
python -c "
from pipeline.emit import merge_event_files
count = merge_event_files('./data/generated_events/all_events.jsonl')
print(f'Merged {count} events → data/generated_events/all_events.jsonl')
"

# ── POST all events to the running API ───────────────────────────────────────

echo ""
echo "--- Ingesting events into API (ensure docker compose up is running) ---"
python pipeline/ingest_events.py

echo ""
echo "=== Pipeline complete ==="
echo "Dashboard: http://localhost:8501"
echo "API docs:  http://localhost:8000/docs"