#!/usr/bin/env bash
# Run the full detection pipeline for both stores, then ingest into the API.
# Usage: bash pipeline/run.sh
# Assumes MP4 files are in data/videos/ with exact filenames as provided.

set -e

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

# -----------------------------------------------
# STORE 1: ST1076  (March 2026 footage)
# Cameras: CAM3=entry, CAM1=zone, CAM2=zone, CAM6=billing
# -----------------------------------------------
echo "--- ST1076: Processing entry camera (CAM3) ---"
STORE_ID=ST1076 python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM3 \
    --video  "data/videos/CAM 3 - entry.mp4" \
    --clip-start "2026-03-08T13:00:00"

echo "--- ST1076: Processing zone camera A (CAM1) ---"
STORE_ID=ST1076 python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM1 \
    --video  "data/videos/CAM 1 - zone.mp4" \
    --clip-start "2026-03-08T13:00:00"

echo "--- ST1076: Processing zone camera B (CAM2) ---"
STORE_ID=ST1076 python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM2 \
    --video  "data/videos/CAM 2 - zone.mp4" \
    --clip-start "2026-03-08T13:00:00"

echo "--- ST1076: Processing billing camera (CAM6) ---"
STORE_ID=ST1076 python pipeline/detect.py \
    --store  ST1076 \
    --camera CAM6 \
    --video  "data/videos/CAM 5 - billing.mp4" \
    --clip-start "2026-03-08T13:00:00"

# -----------------------------------------------
# STORE 2: ST1008  (April 2026 footage)
# Cameras: CAM_ENTRY_1=entry, CAM_ENTRY_2=entry, CAM_ZONE=zone, CAM_BILLING=billing
# -----------------------------------------------
echo "--- ST1008: Processing entry camera 1 ---"
STORE_ID=ST1008 python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_ENTRY_1 \
    --video  "data/videos/entry 1.mp4" \
    --clip-start "2026-04-10T06:30:00"

echo "--- ST1008: Processing entry camera 2 ---"
STORE_ID=ST1008 python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_ENTRY_2 \
    --video  "data/videos/entry 2.mp4" \
    --clip-start "2026-04-10T06:30:00"

echo "--- ST1008: Processing zone camera ---"
STORE_ID=ST1008 python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_ZONE \
    --video  "data/videos/zone.mp4" \
    --clip-start "2026-04-10T06:30:00"

echo "--- ST1008: Processing billing area camera ---"
STORE_ID=ST1008 python pipeline/detect.py \
    --store  ST1008 \
    --camera CAM_BILLING \
    --video  "data/videos/billing_area.mp4" \
    --clip-start "2026-04-10T06:30:00"

# -----------------------------------------------
# Merge all per-camera JSONL files → all_events.jsonl
# -----------------------------------------------
echo ""
echo "--- Merging all event files ---"
python -c "
from pipeline.emit import merge_event_files
count = merge_event_files('./data/generated_events/all_events.jsonl')
print(f'Merged {count} events → data/generated_events/all_events.jsonl')
"

# -----------------------------------------------
# POST events to API in batches of 500
# -----------------------------------------------
echo ""
echo "--- Ingesting events into API ---"
python pipeline/ingest_events.py

echo ""
echo "=== Pipeline complete ==="
echo "Dashboard: http://localhost:8501"
echo "API docs:  http://localhost:8000/docs"