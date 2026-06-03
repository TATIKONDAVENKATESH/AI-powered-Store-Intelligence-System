@echo off
REM ============================================================
REM  Store Intelligence Detection Pipeline (Windows)
REM  Usage: pipeline\run.bat
REM  Prerequisites:
REM    1. docker compose up  (API must be running before ingest step)
REM    2. Place MP4 files in data\videos\ with exact filenames below
REM    3. Place POS CSV at data\pos_transactions.csv
REM ============================================================

cd /d "%~dp0.."

set EVENTS_DIR=.\data\generated_events
set API_URL=http://localhost:8000

if not exist "%EVENTS_DIR%" mkdir "%EVENTS_DIR%"

echo.
echo === Store Intelligence Detection Pipeline ===
echo Project root: %CD%
echo Events dir:   %EVENTS_DIR%
echo.

REM ── STORE 1: ST1076 (March 2026 footage) ──────────────────────────────────

echo --- ST1076: entry camera (CAM3) ---
python pipeline\detect.py --store ST1076 --camera CAM3 ^
    --video "data\videos\Store 1\CAM 3 - entry.mp4" ^
    --clip-start "2026-03-08T13:00:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM3 detection failed or video missing

echo --- ST1076: zone camera A (CAM1) ---
python pipeline\detect.py --store ST1076 --camera CAM1 ^
    --video "data\videos\Store 1\CAM 1 - zone.mp4" ^
    --clip-start "2026-03-08T13:00:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM1 detection failed or video missing

echo --- ST1076: zone camera B (CAM2) ---
python pipeline\detect.py --store ST1076 --camera CAM2 ^
    --video "data\videos\Store 1\CAM 2 - zone.mp4" ^
    --clip-start "2026-03-08T13:00:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM2 detection failed or video missing

echo --- ST1076: billing camera (CAM6) ---
python pipeline\detect.py --store ST1076 --camera CAM6 ^
    --video "data\videos\Store 1\CAM 5 - billing.mp4" ^
    --clip-start "2026-03-08T13:00:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM6 detection failed or video missing

REM ── STORE 2: ST1008 (April 2026 footage) ──────────────────────────────────

echo --- ST1008: entry camera 1 ---
python pipeline\detect.py --store ST1008 --camera CAM_ENTRY_1 ^
    --video "data\videos\Store 2\entry 1.mp4" ^
    --clip-start "2026-04-10T06:30:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM_ENTRY_1 detection failed or video missing

echo --- ST1008: entry camera 2 ---
python pipeline\detect.py --store ST1008 --camera CAM_ENTRY_2 ^
    --video "data\videos\Store 2\entry 2.mp4" ^
    --clip-start "2026-04-10T06:30:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM_ENTRY_2 detection failed or video missing

echo --- ST1008: zone camera ---
python pipeline\detect.py --store ST1008 --camera CAM_ZONE ^
    --video "data\videos\Store 2\zone.mp4" ^
    --clip-start "2026-04-10T06:30:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM_ZONE detection failed or video missing

echo --- ST1008: billing area camera ---
python pipeline\detect.py --store ST1008 --camera CAM_BILLING ^
    --video "data\videos\Store 2\billing_area.mp4" ^
    --clip-start "2026-04-10T06:30:00"
if %ERRORLEVEL% NEQ 0 echo [WARN] CAM_BILLING detection failed or video missing

REM ── Merge all per-camera JSONL files into all_events.jsonl ────────────────

echo.
echo --- Merging event files ---
python -c "from pipeline.emit import merge_event_files; count = merge_event_files('./data/generated_events/all_events.jsonl'); print(f'Merged {count} events')"

REM ── POST all events to API ────────────────────────────────────────────────

echo.
echo --- Ingesting events into API (ensure docker compose up is running) ---
python pipeline\ingest_events.py

echo.
echo === Pipeline complete ===
echo Dashboard: http://localhost:8501
echo API docs:  http://localhost:8000/docs
pause