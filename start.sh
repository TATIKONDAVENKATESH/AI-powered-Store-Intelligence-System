#!/bin/bash

echo "1. Starting FastAPI backend in the background..."
export DATABASE_URL="sqlite+aiosqlite:///./storage/store_intelligence.db"
export POS_CSV="./data/pos_transactions.csv"
export API_URL="http://localhost:8000"

uvicorn app.main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

echo "2. Waiting for API to be healthy..."
sleep 5

echo "3. Ingesting pre-generated events into the database..."
python pipeline/ingest_events.py --file data/generated_events/all_events.jsonl --batch-size 500 --workers 4

echo "4. Starting Streamlit Dashboard..."
export STORE_IDS="ST1076,ST1008"
# Render.com provides a dynamic $PORT. Fallback to 7860 if not set.
DASHBOARD_PORT=${PORT:-7860}
streamlit run dashboard/streamlit_app.py --server.port $DASHBOARD_PORT --server.address 0.0.0.0 --server.headless true
