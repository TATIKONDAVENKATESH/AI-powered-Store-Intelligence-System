from __future__ import annotations
"""
Reads all_events.jsonl (produced by detect.py + merge_event_files) and POSTs
events in batches of 500 to the /events/ingest API endpoint.

Run AFTER detect.py has processed all clips and merge_event_files() has been called.

Module-level names that tests monkeypatch:
  JSONL   — full path to the merged events file
  BATCH   — batch size (alias for BATCH_SIZE)
  API_URL — base URL of the running API
"""
import json
import os
import sys
import urllib.request
import urllib.error

API_URL    = os.getenv("API_URL", "http://localhost:8000")
EVENTS_DIR = os.getenv("EVENTS_DIR", "./data/generated_events")
BATCH_SIZE = 500
BATCH      = BATCH_SIZE   # alias so tests can monkeypatch ingest_mod.BATCH

# Resolve EVENTS_DIR to absolute path using the same logic as emit.py
if not os.path.isabs(EVENTS_DIR):
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    EVENTS_DIR    = os.path.normpath(os.path.join(_project_root, EVENTS_DIR))

JSONL = os.path.join(EVENTS_DIR, "all_events.jsonl")  # tests monkeypatch this


def load_events(path: str) -> list[dict]:
    """Load all events from a JSONL file. Returns empty list if file missing or empty."""
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except FileNotFoundError:
        pass
    return events


def post_batch(events: list[dict], api_url: str) -> dict:
    """POST a batch of events to /events/ingest via stdlib urllib (no extra deps)."""
    payload = json.dumps({"events": events}).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/events/ingest",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> None:
    """Read all_events.jsonl and POST to API in batches of 500."""
    events_path = JSONL
    batch_size  = BATCH

    if not os.path.exists(events_path):
        print(f"No events file at {events_path} — run detect.py and merge first")
        print("Expected path: data/generated_events/all_events.jsonl")
        return

    events = load_events(events_path)
    print(f"Loaded {len(events)} events from {events_path}")

    if not events:
        print("Done — total accepted=0 rejected=0 duplicates=0")
        return

    total_accepted = 0
    total_rejected = 0
    total_dup      = 0

    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        try:
            result = post_batch(batch, API_URL)
            acc = result.get("accepted", 0)
            rej = result.get("rejected", 0)
            dup = result.get("duplicates", 0)
            total_accepted += acc
            total_rejected += rej
            total_dup      += dup
            print(f"Batch {i // batch_size + 1}: accepted={acc} rejected={rej} dup={dup}")
        except urllib.error.URLError as exc:
            print(f"[ERROR] Batch {i // batch_size + 1} failed: {exc}")
            print("  Is the API running? Try: docker compose up  OR  uvicorn app.main:app")
        except Exception as exc:
            print(f"[ERROR] Batch {i // batch_size + 1} unexpected error: {exc}")

    print(f"Total accepted: {total_accepted} | rejected: {total_rejected} | duplicates: {total_dup}")


if __name__ == "__main__":
    main()