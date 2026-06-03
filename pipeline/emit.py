from __future__ import annotations
import json
import uuid
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

STORE_ID   = os.getenv("STORE_ID", "ST1076")        # ST1076 or ST1008
EVENTS_DIR = os.getenv("EVENTS_DIR", "./data/generated_events")


def _utc_iso(clip_start_utc: datetime, frame_idx: int, fps: float) -> str:
    """Convert frame index to UTC ISO-8601 timestamp."""
    offset_seconds = frame_idx / fps
    ts = clip_start_utc + timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(
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
    """Build a single structured event dict matching the required API schema."""
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   STORE_ID,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  _utc_iso(clip_start_utc, frame_idx, fps),
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone":    sku_zone,
            "session_seq": session_seq,
        },
    }


class EventEmitter:
    """Collects events in memory and flushes to a JSONL file per camera."""

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._events: list[dict] = []

    def emit(self, event: dict) -> None:
        self._events.append(event)

    def flush(self, output_path: Optional[str] = None) -> str:
        os.makedirs(EVENTS_DIR, exist_ok=True)
        if output_path is None:
            output_path = os.path.join(EVENTS_DIR, f"{self.camera_id}_events.jsonl")
        with open(output_path, "w", encoding="utf-8") as f:
            for ev in self._events:
                f.write(json.dumps(ev) + "\n")
        return output_path

    def count(self) -> int:
        return len(self._events)


def load_jsonl(path: str) -> list[dict]:
    """Load all events from a JSONL file."""
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def merge_event_files(output_path: str) -> int:
    """Merge all per-camera JSONL files into one chronologically sorted file."""
    all_events: list[dict] = []
    for fname in os.listdir(EVENTS_DIR):
        if fname.endswith("_events.jsonl") and fname != os.path.basename(output_path):
            all_events.extend(load_jsonl(os.path.join(EVENTS_DIR, fname)))
    all_events.sort(key=lambda e: e["timestamp"])
    with open(output_path, "w", encoding="utf-8") as f:
        for ev in all_events:
            f.write(json.dumps(ev) + "\n")
    return len(all_events)