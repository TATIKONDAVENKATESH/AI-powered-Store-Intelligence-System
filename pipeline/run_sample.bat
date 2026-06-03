@echo off

cd /d "%~dp0.."

echo.
echo === Sample Event Ingestion ===
echo.

python pipeline\ingest_events.py data\generated_events\sample_events.jsonl

echo.
echo Done.
pause