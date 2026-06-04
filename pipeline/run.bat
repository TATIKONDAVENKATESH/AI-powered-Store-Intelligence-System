@echo off

cd /d "%~dp0.."

set EVENTS_FILE=data\generated_events\all_events.jsonl

echo.
echo === Store Intelligence Event Replay ===
echo.

if not exist "%EVENTS_FILE%" (
    echo [ERROR] %EVENTS_FILE% not found
    pause
    exit /b 1
)

echo Ingesting events from:
echo %EVENTS_FILE%
echo.

python pipeline\ingest_events.py "%EVENTS_FILE%"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Event ingestion failed
    pause
    exit /b 1
)

echo.
echo === Ingestion Complete ===
echo.
echo Dashboard: http://localhost:8501
echo API Docs:  http://localhost:8000/docs
echo.

pause