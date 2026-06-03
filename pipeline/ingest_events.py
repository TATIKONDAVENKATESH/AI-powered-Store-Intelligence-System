from __future__ import annotations
"""
Reads all_events.jsonl and POSTs in batches of 500 to the ingest endpoint.
Run after detect.py has processed all clips.
"""
import json
import os
import sys
import time

import requests

API_URL    = os.getenv("API_URL", "http://localhost:8000")
EVENTS_DIR = os.getenv("EVENTS_DIR", "./data/generated_events")
BATCH_SIZE = 500


def load_all_events(path: str) -> list[dict]:
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def post_batch(events: list[dict]) -> dict:
    resp = requests.post(
        f"{API_URL}/events/ingest",
        json={"events": events},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    merged_path = os.path.join(EVENTS_DIR, "all_events.jsonl")
    if not os.path.exists(merged_path):
        print(f"No merged events file found at {merged_path}")
        print("Run merge first:")
        print('  python -c "from pipeline.emit import merge_event_files; merge_event_files(\'./data/generated_events/all_events.jsonl\')"')
        sys.exit(1)

    events = load_all_events(merged_path)
    print(f"Loaded {len(events)} events from {merged_path}")

    total_accepted  = 0
    total_rejected  = 0
    total_dup       = 0

    for i in range(0, len(events), BATCH_SIZE):
        batch = events[i:i + BATCH_SIZE]
        try:
            result = post_batch(batch)
            total_accepted += result.get("accepted", 0)
            total_rejected += result.get("rejected", 0)
            total_dup      += result.get("duplicates", 0)
            print(f"Batch {i//BATCH_SIZE + 1}: accepted={result['accepted']} rejected={result['rejected']} dup={result['duplicates']}")
        except Exception as exc:
            print(f"Batch {i//BATCH_SIZE + 1} failed: {exc}")
        time.sleep(0.05)  # small delay to avoid overwhelming the API

    print(f"\nDone — total accepted={total_accepted} rejected={total_rejected} duplicates={total_dup}")


if __name__ == "__main__":
    main()