#!/usr/bin/env bash
# run.sh — Process all 5 camera videos then ingest events into the API.
# Usage: bash pipeline/run.sh
# Requires: API running at localhost:8000 (docker compose up or uvicorn)

set -e

VIDEO_DIR="${VIDEO_DIR:-./data/videos}"
EVENTS_DIR="${EVENTS_DIR:-./data/generated_events}"
API_URL="${API_URL:-http://localhost:8000}"
LAYOUT_JSON="${LAYOUT_JSON:-./config/store_layout.json}"
YOLO_MODEL="${YOLO_MODEL:-yolov8n.pt}"
YOLO_CONFIDENCE="${YOLO_CONFIDENCE:-0.4}"

export LAYOUT_JSON YOLO_MODEL YOLO_CONFIDENCE EVENTS_DIR

echo "=== Store Intelligence — Detection Pipeline ==="
echo "Video dir : $VIDEO_DIR"
echo "Events dir: $EVENTS_DIR"
echo "API       : $API_URL"
echo ""

mkdir -p "$EVENTS_DIR"

# Map camera_id → video filename (from store_layout.json)
declare -A CAMERA_MAP=(
    ["CAM_ENTRY_01"]="entry_camera.mp4"
    ["CAM_FLOOR_A"]="central_a.mp4"
    ["CAM_FLOOR_B"]="central_b.mp4"
    ["CAM_BILLING_01"]="billing_camera.mp4"
    ["CAM_STAFF_01"]="staff_room_camera.mp4"
)

# Step 1: Run detection on each camera
for CAM_ID in "${!CAMERA_MAP[@]}"; do
    VIDEO_FILE="${VIDEO_DIR}/${CAMERA_MAP[$CAM_ID]}"
    if [ ! -f "$VIDEO_FILE" ]; then
        echo "[SKIP] $CAM_ID — video not found: $VIDEO_FILE"
        continue
    fi
    echo "[DETECT] $CAM_ID → $VIDEO_FILE"
    python pipeline/detect.py --camera "$CAM_ID" --video "$VIDEO_FILE"
    echo "[DONE]   $CAM_ID"
done

echo ""
echo "=== Detection complete. Merging events... ==="

# Step 2: Merge all per-camera JSONL files into one sorted file
python - << 'PYEOF'
import sys, os
sys.path.insert(0, ".")
from pipeline.emit import merge_event_files
out = "./data/generated_events/all_events.jsonl"
n = merge_event_files(out)
print(f"Merged {n} events → {out}")
PYEOF

echo ""
echo "=== Ingesting events into API at $API_URL ==="

# Step 3: Batch-ingest events into POST /events/ingest (batches of 500)
python - << 'PYEOF'
import json, sys, os, math
import urllib.request, urllib.error

API_URL   = os.getenv("API_URL", "http://localhost:8000")
JSONL     = "./data/generated_events/all_events.jsonl"
BATCH     = 500

if not os.path.exists(JSONL):
    print("No events file found — skipping ingest")
    sys.exit(0)

with open(JSONL) as f:
    events = [json.loads(l) for l in f if l.strip()]

print(f"Ingesting {len(events)} events in batches of {BATCH}...")

total_accepted = 0
for i in range(0, len(events), BATCH):
    batch = events[i:i+BATCH]
    body  = json.dumps({"events": batch}).encode()
    req   = urllib.request.Request(
        f"{API_URL}/events/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            total_accepted += result.get("accepted", 0)
            print(f"  Batch {i//BATCH+1}: accepted={result['accepted']} "
                  f"rejected={result['rejected']} duplicates={result['duplicates']}")
    except urllib.error.URLError as e:
        print(f"  [ERROR] Batch {i//BATCH+1}: {e}")

print(f"\nDone. Total accepted: {total_accepted}")
PYEOF

echo ""
echo "=== Pipeline complete ==="
echo "Check metrics: curl $API_URL/stores/STORE_BLR_002/metrics"