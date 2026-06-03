#!/usr/bin/env bash

# ============================================================
# Store Intelligence Sample Event Ingestion
# Usage: bash pipeline/run_sample.sh
#
# Prerequisites:
#   1. docker compose up
#   2. data/generated_events/sample_events.jsonl exists
# ============================================================

set -e

cd "$(dirname "$0")/.."

echo
echo "=== Sample Event Ingestion ==="
echo "Project root: $(pwd)"
echo

if [ ! -f "data/sample_events.jsonl" ]; then
    echo "[ERROR] data/sample_events.jsonl not found"
    exit 1
fi

echo "--- Ingesting sample events into API ---"
python pipeline/ingest_events.py data/generated_events/sample_events.jsonl

echo
echo "=== Complete ==="
echo "Dashboard: http://localhost:8501"
echo "API docs:  http://localhost:8000/docs"