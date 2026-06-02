@echo off
REM run.bat — Process all 5 camera videos then ingest events into the API.
REM Usage: pipeline\run.bat
REM Requires: API running at localhost:8000

setlocal EnableDelayedExpansion

set VIDEO_DIR=%VIDEO_DIR%
if "%VIDEO_DIR%"=="" set VIDEO_DIR=.\data\videos

set EVENTS_DIR=%EVENTS_DIR%
if "%EVENTS_DIR%"=="" set EVENTS_DIR=.\data\generated_events

set API_URL=%API_URL%
if "%API_URL%"=="" set API_URL=http://localhost:8000

set LAYOUT_JSON=%LAYOUT_JSON%
if "%LAYOUT_JSON%"=="" set LAYOUT_JSON=.\config\store_layout.json

set YOLO_MODEL=%YOLO_MODEL%
if "%YOLO_MODEL%"=="" set YOLO_MODEL=yolov8n.pt

set YOLO_CONFIDENCE=%YOLO_CONFIDENCE%
if "%YOLO_CONFIDENCE%"=="" set YOLO_CONFIDENCE=0.4

echo === Store Intelligence - Detection Pipeline ===
echo Video dir : %VIDEO_DIR%
echo Events dir: %EVENTS_DIR%
echo API       : %API_URL%
echo.

if not exist "%EVENTS_DIR%" mkdir "%EVENTS_DIR%"

REM --- Step 1: Detect per camera ---
echo [DETECT] CAM_ENTRY_01
if exist "%VIDEO_DIR%\entry_camera.mp4" (
    python pipeline\detect.py --camera CAM_ENTRY_01 --video "%VIDEO_DIR%\entry_camera.mp4"
) else (
    echo [SKIP] entry_camera.mp4 not found
)

echo [DETECT] CAM_FLOOR_A
if exist "%VIDEO_DIR%\central_a.mp4" (
    python pipeline\detect.py --camera CAM_FLOOR_A --video "%VIDEO_DIR%\central_a.mp4"
) else (
    echo [SKIP] central_a.mp4 not found
)

echo [DETECT] CAM_FLOOR_B
if exist "%VIDEO_DIR%\central_b.mp4" (
    python pipeline\detect.py --camera CAM_FLOOR_B --video "%VIDEO_DIR%\central_b.mp4"
) else (
    echo [SKIP] central_b.mp4 not found
)

echo [DETECT] CAM_BILLING_01
if exist "%VIDEO_DIR%\billing_camera.mp4" (
    python pipeline\detect.py --camera CAM_BILLING_01 --video "%VIDEO_DIR%\billing_camera.mp4"
) else (
    echo [SKIP] billing_camera.mp4 not found
)

echo [DETECT] CAM_STAFF_01
if exist "%VIDEO_DIR%\staff_room_camera.mp4" (
    python pipeline\detect.py --camera CAM_STAFF_01 --video "%VIDEO_DIR%\staff_room_camera.mp4"
) else (
    echo [SKIP] staff_room_camera.mp4 not found
)

echo.
echo === Detection complete. Merging events... ===

python -c "import sys; sys.path.insert(0,'.'); from pipeline.emit import merge_event_files; n=merge_event_files('./data/generated_events/all_events.jsonl'); print(f'Merged {n} events')"

echo.
echo === Ingesting events into API at %API_URL% ===

python pipeline\ingest_events.py

echo.
echo === Pipeline complete ===
echo Check metrics: curl %API_URL%/stores/STORE_BLR_002/metrics