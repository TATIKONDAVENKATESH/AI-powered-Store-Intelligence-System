"""
test_pipeline.py — Tests for Pydantic models, EventEmitter, and ReIDTracker

Covers:
  - StoreEvent model: all 8 event types, validation rules, defaults
  - IngestRequest: max 500 events batch limit
  - EventEmitter: buffer, flush, count
  - build_event: timestamp generation from frame index
  - ReIDTracker: new visitor assignment, re-entry detection, exit marking
  - merge_event_files: chronological sort and skip self

No YOLO, OpenCV, GPU, or network required.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
import time
import pytest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError
from app.models import StoreEvent, EventMetadata, IngestRequest


# ── StoreEvent model tests ────────────────────────────────────────────────────

def _base_event(**overrides) -> dict:
    """Valid minimal event dict."""
    base = {
        "event_id":   str(uuid.uuid4()),
        "store_id":   "ST1076",
        "camera_id":  "CAM3",
        "visitor_id": "VIS_0001",
        "event_type": "ENTRY",
        "timestamp":  "2026-03-08T13:00:00Z",
        "confidence": 0.85,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("event_type", [
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
])
def test_all_eight_event_types_accepted(event_type):
    """All 8 event types in the spec must be accepted by the model."""
    ev = StoreEvent(**_base_event(event_type=event_type))
    assert ev.event_type == event_type


def test_invalid_event_type_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(event_type="BROWSE"))


def test_invalid_timestamp_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(timestamp="not-a-date"))


def test_invalid_uuid_event_id_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(event_id="not-a-uuid"))


def test_confidence_above_one_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(confidence=1.01))


def test_confidence_below_zero_rejected():
    with pytest.raises(ValidationError):
        StoreEvent(**_base_event(confidence=-0.01))


def test_confidence_exactly_zero_accepted():
    ev = StoreEvent(**_base_event(confidence=0.0))
    assert ev.confidence == 0.0


def test_confidence_exactly_one_accepted():
    ev = StoreEvent(**_base_event(confidence=1.0))
    assert ev.confidence == 1.0


def test_is_staff_defaults_to_false():
    ev = StoreEvent(**_base_event())
    assert ev.is_staff is False


def test_dwell_ms_defaults_to_zero():
    ev = StoreEvent(**_base_event())
    assert ev.dwell_ms == 0


def test_zone_id_defaults_to_none():
    ev = StoreEvent(**_base_event())
    assert ev.zone_id is None


def test_metadata_defaults():
    ev = StoreEvent(**_base_event())
    assert ev.metadata.queue_depth is None
    assert ev.metadata.sku_zone is None
    assert ev.metadata.session_seq == 0


def test_zone_event_with_zone_id():
    ev = StoreEvent(**_base_event(event_type="ZONE_ENTER", zone_id="ST1076_Z01"))
    assert ev.zone_id == "ST1076_Z01"


def test_billing_queue_with_metadata():
    ev = StoreEvent(**_base_event(
        event_type="BILLING_QUEUE_JOIN",
        zone_id="ST1076_Z_BILLING_01",
        metadata={"queue_depth": 5, "sku_zone": None, "session_seq": 3},
    ))
    assert ev.metadata.queue_depth == 5
    assert ev.metadata.session_seq == 3


def test_auto_generated_event_id():
    """event_id should be auto-generated as a valid UUID when not provided."""
    data = _base_event()
    del data["event_id"]
    ev = StoreEvent(**data)
    uuid.UUID(ev.event_id)  # must parse without error


def test_store_id_st1008():
    ev = StoreEvent(**_base_event(store_id="ST1008", camera_id="CAM_ENTRY_1"))
    assert ev.store_id == "ST1008"


def test_timestamp_with_z_suffix_accepted():
    ev = StoreEvent(**_base_event(timestamp="2026-03-08T13:00:00Z"))
    assert "2026" in ev.timestamp


def test_timestamp_with_offset_accepted():
    ev = StoreEvent(**_base_event(timestamp="2026-03-08T13:00:00+00:00"))
    assert ev.timestamp == "2026-03-08T13:00:00+00:00"


# ── IngestRequest batch limit ─────────────────────────────────────────────────

def test_ingest_request_over_500_events_rejected():
    """IngestRequest must reject batches larger than 500 events."""
    events = [StoreEvent(**_base_event(event_id=str(uuid.uuid4()))) for _ in range(501)]
    with pytest.raises(ValidationError) as exc_info:
        IngestRequest(events=events)
    assert "500" in str(exc_info.value)


def test_ingest_request_exactly_500_events_accepted():
    events = [StoreEvent(**_base_event(event_id=str(uuid.uuid4()))) for _ in range(500)]
    req = IngestRequest(events=events)
    assert len(req.events) == 500


def test_ingest_request_empty_accepted():
    req = IngestRequest(events=[])
    assert req.events == []


# ── EventEmitter tests ─────────────────────────────────────────────────────────

def test_event_emitter_buffer_and_count():
    from pipeline.emit import EventEmitter
    emitter = EventEmitter("CAM_TEST")
    assert emitter.count() == 0
    ev = {"event_id": str(uuid.uuid4()), "event_type": "ENTRY"}
    emitter.emit(ev)
    emitter.emit(ev)
    assert emitter.count() == 2


def test_event_emitter_flush_writes_jsonl(tmp_path):
    from pipeline.emit import EventEmitter
    emitter = EventEmitter("CAM_FLUSH")
    ev1 = {"event_id": str(uuid.uuid4()), "event_type": "ENTRY"}
    ev2 = {"event_id": str(uuid.uuid4()), "event_type": "EXIT"}
    emitter.emit(ev1)
    emitter.emit(ev2)
    out = str(tmp_path / "out.jsonl")
    path = emitter.flush(output_path=out)
    assert path == out
    lines = open(out).read().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "ENTRY"
    assert json.loads(lines[1])["event_type"] == "EXIT"


def test_event_emitter_flush_empty(tmp_path):
    from pipeline.emit import EventEmitter
    emitter = EventEmitter("CAM_EMPTY")
    out = str(tmp_path / "empty.jsonl")
    emitter.flush(output_path=out)
    assert open(out).read() == ""


# ── build_event tests ─────────────────────────────────────────────────────────

def test_build_event_timestamp_from_frame():
    """build_event converts frame index to correct ISO-8601 UTC timestamp."""
    from pipeline.emit import build_event
    clip_start = datetime(2026, 3, 8, 13, 0, 0, tzinfo=timezone.utc)
    ev = build_event(
        store_id="ST1076",
        camera_id="CAM3",
        visitor_id="VIS_0001",
        event_type="ENTRY",
        frame_idx=30,    # 30 frames at 15 fps = 2 seconds
        fps=15.0,
        clip_start_utc=clip_start,
    )
    assert ev["timestamp"] == "2026-03-08T13:00:02Z"
    assert ev["store_id"] == "ST1076"
    assert ev["event_type"] == "ENTRY"


def test_build_event_has_all_required_fields():
    """build_event must produce a dict with all fields matching the API schema."""
    from pipeline.emit import build_event
    clip_start = datetime(2026, 4, 10, 6, 30, 0, tzinfo=timezone.utc)
    ev = build_event(
        store_id="ST1008",
        camera_id="CAM_ENTRY_1",
        visitor_id="VIS_0005",
        event_type="ZONE_ENTER",
        frame_idx=0,
        fps=25.0,
        clip_start_utc=clip_start,
        zone_id="Z_SKINCARE",
        dwell_ms=0,
        is_staff=False,
        confidence=0.92,
        queue_depth=None,
        sku_zone="SKINCARE",
        session_seq=1,
    )
    required = ["event_id", "store_id", "camera_id", "visitor_id", "event_type",
                "timestamp", "zone_id", "dwell_ms", "is_staff", "confidence", "metadata"]
    for field in required:
        assert field in ev, f"Missing field: {field}"
    assert ev["metadata"]["sku_zone"] == "SKINCARE"
    assert ev["metadata"]["session_seq"] == 1


def test_build_event_uuid_is_unique():
    """Each call to build_event must produce a distinct event_id."""
    from pipeline.emit import build_event
    clip_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    e1 = build_event("ST1076", "CAM3", "V1", "ENTRY", 0, 15.0, clip_start)
    e2 = build_event("ST1076", "CAM3", "V1", "ENTRY", 0, 15.0, clip_start)
    assert e1["event_id"] != e2["event_id"]


# ── load_jsonl tests ───────────────────────────────────────────────────────────

def test_load_jsonl_valid_file(tmp_path):
    from pipeline.emit import load_jsonl
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event_id":"1","event_type":"ENTRY"}\n'
        '{"event_id":"2","event_type":"EXIT"}\n'
    )
    events = load_jsonl(str(f))
    assert len(events) == 2
    assert events[0]["event_type"] == "ENTRY"


def test_load_jsonl_missing_file_returns_empty():
    from pipeline.emit import load_jsonl
    events = load_jsonl("/nonexistent/path/events.jsonl")
    assert events == []


def test_load_jsonl_skips_malformed_lines(tmp_path):
    from pipeline.emit import load_jsonl
    f = tmp_path / "bad.jsonl"
    f.write_text('{"event_id":"1"}\nNOT_JSON\n{"event_id":"3"}\n')
    events = load_jsonl(str(f))
    assert len(events) == 2


# ── ReIDTracker tests ─────────────────────────────────────────────────────────

def test_reid_new_track_gets_new_visitor_id():
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    vid, is_reentry = reid.get_visitor_id(track_id=1, centroid=(100.0, 200.0))
    assert vid.startswith("VIS_")
    assert is_reentry is False


def test_reid_same_track_same_visitor_id():
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    vid1, _ = reid.get_visitor_id(1, (100.0, 200.0))
    vid2, _ = reid.get_visitor_id(1, (105.0, 205.0))
    assert vid1 == vid2


def test_reid_different_tracks_different_ids():
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    vid1, _ = reid.get_visitor_id(1, (100.0, 200.0))
    vid2, _ = reid.get_visitor_id(2, (500.0, 500.0))  # far apart
    assert vid1 != vid2


def test_reid_reentry_detected_within_window():
    """
    Track exits near centroid (100, 100); new track appears within REENTRY_DIST_PX
    within REENTRY_WINDOW_S → re-entry detected, same visitor_id returned.
    """
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    vid1, _ = reid.get_visitor_id(track_id=10, centroid=(100.0, 100.0))
    reid.mark_exit(track_id=10, centroid=(100.0, 100.0))
    # New track appears near same centroid
    vid2, is_reentry = reid.get_visitor_id(track_id=99, centroid=(110.0, 110.0))
    assert is_reentry is True
    assert vid2 == vid1


def test_reid_no_reentry_when_far_away():
    """Track exits at (100, 100); new track at (1000, 1000) — too far → new visitor."""
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    vid1, _ = reid.get_visitor_id(10, (100.0, 100.0))
    reid.mark_exit(10, (100.0, 100.0))
    vid2, is_reentry = reid.get_visitor_id(99, (1000.0, 1000.0))
    assert is_reentry is False
    assert vid2 != vid1


def test_reid_get_last_centroid_returns_none_for_unknown_track():
    """get_last_centroid must return None for a track that was never seen."""
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    assert reid.get_last_centroid(track_id=9999) is None


def test_reid_get_last_centroid_returns_most_recent():
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    reid.get_visitor_id(5, (10.0, 20.0))
    reid.get_visitor_id(5, (15.0, 25.0))  # updates centroid
    cx, cy = reid.get_last_centroid(5)
    assert (cx, cy) == (15.0, 25.0)


def test_reid_monotonic_visitor_ids():
    """Visitor IDs should be sequentially numbered."""
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()
    v1, _ = reid.get_visitor_id(1, (0.0, 0.0))
    v2, _ = reid.get_visitor_id(2, (1000.0, 1000.0))
    v3, _ = reid.get_visitor_id(3, (2000.0, 2000.0))
    assert v1 == "VIS_0001"
    assert v2 == "VIS_0002"
    assert v3 == "VIS_0003"


# ── merge_event_files tests ───────────────────────────────────────────────────

def test_merge_event_files_chronological_sort(tmp_path, monkeypatch):
    """merge_event_files must produce events sorted by timestamp."""
    from pipeline import emit as emit_mod
    monkeypatch.setattr(emit_mod, "EVENTS_DIR", str(tmp_path))

    # Write two camera files with out-of-order timestamps
    (tmp_path / "CAM_A_events.jsonl").write_text(
        '{"timestamp":"2026-03-08T13:00:10Z","event_type":"EXIT"}\n'
        '{"timestamp":"2026-03-08T13:00:30Z","event_type":"EXIT"}\n'
    )
    (tmp_path / "CAM_B_events.jsonl").write_text(
        '{"timestamp":"2026-03-08T13:00:05Z","event_type":"ENTRY"}\n'
        '{"timestamp":"2026-03-08T13:00:20Z","event_type":"ZONE_ENTER"}\n'
    )

    out = str(tmp_path / "all_events.jsonl")
    count = emit_mod.merge_event_files(out)
    assert count == 4

    events = emit_mod.load_jsonl(out)
    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps)


def test_merge_event_files_skips_output_file_itself(tmp_path, monkeypatch):
    """merge_event_files must not include the output file (all_events.jsonl) in the merge."""
    from pipeline import emit as emit_mod
    monkeypatch.setattr(emit_mod, "EVENTS_DIR", str(tmp_path))

    (tmp_path / "CAM_C_events.jsonl").write_text(
        '{"timestamp":"2026-03-08T13:00:01Z","event_type":"ENTRY"}\n'
    )
    # Pre-existing all_events.jsonl from a previous run — should be ignored
    (tmp_path / "all_events.jsonl").write_text(
        '{"timestamp":"2026-03-08T13:00:00Z","event_type":"ENTRY"}\n'
        '{"timestamp":"2026-03-08T13:00:00Z","event_type":"ENTRY"}\n'
    )

    out = str(tmp_path / "all_events.jsonl")
    count = emit_mod.merge_event_files(out)
    # Only CAM_C_events.jsonl (1 event), not the old all_events.jsonl
    assert count == 1