"""
ingest_events.py — Batch-ingest all_events.jsonl into the API.
Called by run.bat; can also be run directly:
    python pipeline/ingest_events.py
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request
import urllib.error

API_URL   = os.getenv("API_URL", "http://localhost:8000")
JSONL     = os.getenv("EVENTS_JSONL", "./data/generated_events/all_events.jsonl")
BATCH     = 500


def main() -> None:
    if not os.path.exists(JSONL):
        print(f"No events file at {JSONL} — skipping ingest")
        return

    with open(JSONL) as f:
        events = [json.loads(line) for line in f if line.strip()]

    print(f"Ingesting {len(events)} events in batches of {BATCH}...")
    total_accepted = 0

    for i in range(0, len(events), BATCH):
        batch = events[i : i + BATCH]
        body  = json.dumps({"events": batch}).encode()
        req   = urllib.request.Request(
            f"{API_URL}/events/ingest",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                total_accepted += result.get("accepted", 0)
                print(
                    f"  Batch {i // BATCH + 1}: "
                    f"accepted={result['accepted']} "
                    f"rejected={result['rejected']} "
                    f"duplicates={result['duplicates']}"
                )
        except urllib.error.URLError as exc:
            print(f"  [ERROR] Batch {i // BATCH + 1}: {exc}")

    print(f"\nDone. Total accepted: {total_accepted}")


if __name__ == "__main__":
    main()