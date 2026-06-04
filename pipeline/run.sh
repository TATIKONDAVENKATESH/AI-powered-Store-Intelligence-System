#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

EVENTS_FILE="./data/generated_events/all_events.jsonl"

echo "=== Store Intelligence Event Replay ==="
echo

if [ ! -f "$EVENTS_FILE" ]; then
echo "[ERROR] $EVENTS_FILE not found"
exit 1
fi

echo "Ingesting events from:"
echo "$EVENTS_FILE"
echo

python pipeline/ingest_events.py "$EVENTS_FILE"

echo
echo "=== Ingestion Complete ==="
echo
echo "Dashboard: http://localhost:8501"
echo "API Docs:  http://localhost:8000/docs"
echo