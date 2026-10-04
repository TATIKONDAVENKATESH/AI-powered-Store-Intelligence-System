#!/bin/bash

export DATABASE_URL="sqlite+aiosqlite:///./storage/store_intelligence.db"
export POS_CSV="./data/pos_transactions.csv"
export API_URL="http://localhost:8000"

echo "1. Starting FastAPI backend in the background..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 &

echo "2. Starting Streamlit Dashboard..."
export STORE_IDS="ST1076,ST1008"
DASHBOARD_PORT=${PORT:-7860}
streamlit run dashboard/streamlit_app.py --server.port $DASHBOARD_PORT --server.address 0.0.0.0 --server.headless true &

echo "3. Waiting for API to be healthy..."
while ! curl -s http://localhost:8000/health > /dev/null; do
    echo "Waiting for API..."
    sleep 2
done

echo "4. API is up. Ingesting pre-generated events in the background..."
python pipeline/ingest_events.py --file data/generated_events/all_events.jsonl --batch-size 500 --workers 2 &

# Wait for all background processes to keep container alive
wait -n

