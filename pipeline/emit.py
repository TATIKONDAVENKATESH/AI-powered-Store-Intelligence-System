from __future__ import annotations
"""
Event construction, JSONL buffering, and merge utilities.

KEY FIX: EVENTS_DIR is resolved to an absolute path at module load time.
Previously, when run.bat cd'd to the project root and ran `python pipeline\\detect.py`,
the relative path `./data/generated_events` resolved correctly only if the CWD was the
project root. Now we compute the canonical path once so it is correct regardless of CWD.
"""
import json
import uuid
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

# Resolve to absolute path once at import time to avoid CWD-dependent bugs
_RAW_EVENTS_DIR = os.getenv("EVENTS_DIR", "./data/generated_events")
# If the env var is relative, resolve it relative to the project root
# (two levels up from this file: pipeline/ → project root)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isabs(_RAW_EVENTS_DIR):
    EVENTS_DIR = os.path.normpath(os.path.join(_PROJECT_ROOT, _RAW_EVENTS_DIR))
else:
    EVENTS_DIR = _RAW_EVENTS_DIR


def _utc_iso(clip_start_utc: datetime, frame_idx: int, fps: float) -> str:
    """Convert frame index to UTC ISO-8601 timestamp string."""
    offset_seconds = frame_idx / fps
    ts = clip_start_utc + timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    frame_idx: int,
    fps: float,
    clip_start_utc: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
) -> dict:
    """Build a structured event dict matching the challenge API schema exactly."""
    return {
        "event_id":   str(uuid.uuid4()),   # globally unique UUID v4
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  _utc_iso(clip_start_utc, frame_idx, fps),  # ISO-8601 UTC
        "zone_id":    zone_id,             # null for ENTRY/EXIT events
        "dwell_ms":   dwell_ms,            # 0 for instantaneous events
        "is_staff":   is_staff,            # your model must classify this
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,    # integer for BILLING_QUEUE_JOIN, else null
            "sku_zone":    sku_zone,       # zone label from store_layout.json
            "session_seq": session_seq,    # ordinal position in visitor session
        },
    }


class EventEmitter:
    """Buffers events in memory and flushes to a per-camera JSONL file at clip end."""

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._events: list[dict] = []

    def emit(self, event: dict) -> None:
        """Append one event to the in-memory buffer."""
        self._events.append(event)

    def flush(self, output_path: Optional[str] = None) -> str:
        """
        Write all buffered events to JSONL.

        FIX: os.makedirs is called here with the resolved absolute EVENTS_DIR,
        so the directory is always created correctly regardless of CWD.
        """
        os.makedirs(EVENTS_DIR, exist_ok=True)
        if output_path is None:
            output_path = os.path.join(EVENTS_DIR, f"{self.camera_id}_events.jsonl")
        with open(output_path, "w", encoding="utf-8") as f:
            for ev in self._events:
                f.write(json.dumps(ev) + "\n")
        return output_path

    def count(self) -> int:
        """Return the number of buffered events."""
        return len(self._events)


def load_jsonl(path: str) -> list[dict]:
    """Load events from a JSONL file. Returns empty list on missing or malformed file."""
    events = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # skip malformed lines silently
    except FileNotFoundError:
        pass
    return events


def merge_event_files(output_path: str) -> int:
    """
    Merge all per-camera JSONL files into one chronologically sorted file.

    Skips the output file itself to avoid self-inclusion on repeated runs.
    Returns the total number of merged events.
    """
    os.makedirs(EVENTS_DIR, exist_ok=True)
    all_events: list[dict] = []
    output_basename = os.path.basename(output_path)

    for fname in sorted(os.listdir(EVENTS_DIR)):
        # Only merge per-camera files (end with _events.jsonl)
        # Skip the merged output file itself
        if fname.endswith("_events.jsonl") and fname != output_basename:
            full_path = os.path.join(EVENTS_DIR, fname)
            loaded = load_jsonl(full_path)
            print(f"  merge: {fname} → {len(loaded)} events")
            all_events.extend(loaded)

    # Sort chronologically by ISO-8601 timestamp (lexicographic sort works correctly)
    all_events.sort(key=lambda e: e.get("timestamp", ""))

    with open(output_path, "w", encoding="utf-8") as f:
        for ev in all_events:
            f.write(json.dumps(ev) + "\n")

    print(f"  merged {len(all_events)} events → {output_path}")
    return len(all_events)