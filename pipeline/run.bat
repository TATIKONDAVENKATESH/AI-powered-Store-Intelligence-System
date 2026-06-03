@echo off
REM Run the full detection pipeline for both stores, then ingest into the API.
REM Usage: pipeline\run.bat

cd /d "%~dp0.."
set EVENTS_DIR=.\data\generated_events
set API_URL=http://localhost:8000
if not exist "%EVENTS_DIR%" mkdir "%EVENTS_DIR%"

echo === Store Intelligence Detection Pipeline ===

REM --- ST1076 ---
echo --- ST1076: entry camera (CAM3) ---
set STORE_ID=ST1076
python pipeline\detect.py --store ST1076 --camera CAM3 --video "data\videos\CAM 3 - entry.mp4" --clip-start "2026-03-08T13:00:00"

echo --- ST1076: zone camera A (CAM1) ---
python pipeline\detect.py --store ST1076 --camera CAM1 --video "data\videos\CAM 1 - zone.mp4" --clip-start "2026-03-08T13:00:00"

echo --- ST1076: zone camera B (CAM2) ---
python pipeline\detect.py --store ST1076 --camera CAM2 --video "data\videos\CAM 2 - zone.mp4" --clip-start "2026-03-08T13:00:00"

echo --- ST1076: billing camera (CAM6) ---
python pipeline\detect.py --store ST1076 --camera CAM6 --video "data\videos\CAM 5 - billing.mp4" --clip-start "2026-03-08T13:00:00"

REM --- ST1008 ---
echo --- ST1008: entry camera 1 ---
set STORE_ID=ST1008
python pipeline\detect.py --store ST1008 --camera CAM_ENTRY_1 --video "data\videos\entry 1.mp4" --clip-start "2026-04-10T06:30:00"

echo --- ST1008: entry camera 2 ---
python pipeline\detect.py --store ST1008 --camera CAM_ENTRY_2 --video "data\videos\entry 2.mp4" --clip-start "2026-04-10T06:30:00"

echo --- ST1008: zone camera ---
python pipeline\detect.py --store ST1008 --camera CAM_ZONE --video "data\videos\zone.mp4" --clip-start "2026-04-10T06:30:00"

echo --- ST1008: billing area camera ---
python pipeline\detect.py --store ST1008 --camera CAM_BILLING --video "data\videos\billing_area.mp4" --clip-start "2026-04-10T06:30:00"

REM --- Merge ---
echo --- Merging event files ---
python -c "from pipeline.emit import merge_event_files; count = merge_event_files('./data/generated_events/all_events.jsonl'); print(f'Merged {count} events')"

REM --- Ingest ---
echo --- Ingesting into API ---
python pipeline\ingest_events.py

echo === Pipeline complete ===
pause