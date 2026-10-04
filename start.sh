#!/bin/bash

export DATABASE_URL="sqlite+aiosqlite:///./storage/store_intelligence.db"
export POS_CSV="./data/pos_transactions.csv"
export API_URL="http://localhost:8000"

# 1. Start FastAPI in background
echo "1. Starting FastAPI backend in the background..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 &

# 2. Background task: Wait for API and then ingest events
(
    echo "3. Waiting for API to be healthy..."
    while ! python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" 2>/dev/null; do
        sleep 2
    done
    echo "4. API is up. Ingesting pre-generated events..."
    python pipeline/ingest_events.py --file data/generated_events/all_events.jsonl --batch-size 500 --workers 2
) &

# 3. Start Streamlit in FOREGROUND (keeps container alive)
echo "2. Starting Streamlit Dashboard..."
export STORE_IDS="ST1076,ST1008"
DASHBOARD_PORT=${PORT:-7860}
streamlit run dashboard/streamlit_app.py --server.port $DASHBOARD_PORT --server.address 0.0.0.0 --server.headless true

