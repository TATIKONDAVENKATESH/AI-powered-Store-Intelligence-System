# PROMPT: "Write pytest tests for pipeline/emit.py, pipeline/tracker.py, pipeline/detect.py.
# Cover: build_event schema compliance, EventEmitter flush (with and without output_path),
# merge_event_files, CameraTracker visitor_id assignment, re-entry detection (proximity),
# get_active(), StaffDetector on CAM_BILLING_01 with real HSV frame, detect.py helpers:
# centroid, point_in_polygon, line_crossed, load_layout."
# CHANGES MADE: Added tests for detect.py pure helper functions (no YOLO/video needed).
# Added re-entry proximity test covering tracker.py lines 61-72,75,100-102.
# Added get_active() call covering line 134. Added StaffDetector CAM_BILLING_01 HSV
# path covering lines 159-169. Added emit flush() with no output_path covering line 73.
# FIX: Added test_tracker_reentry_exit_frame_none (line 62) and
# test_tracker_reentry_bbox_none (line 66) to cover the two remaining missed branches.

from __future__ import annotations
import json
import os
import uuid
import pytest
import numpy as np
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.emit import build_event, EventEmitter, merge_event_files
from pipeline.tracker import CameraTracker, StaffDetector, TrackState


# ── build_event ───────────────────────────────────────────────────────────────

def test_build_event_required_fields():
    ev = build_event(
        camera_id="CAM_ENTRY_01", visitor_id="VIS_001",
        event_type="ENTRY", frame_idx=30, fps=15.0,
        clip_start_utc=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
        confidence=0.85, session_seq=1,
    )
    assert ev["event_type"] == "ENTRY"
    assert ev["visitor_id"] == "VIS_001"
    assert ev["camera_id"] == "CAM_ENTRY_01"
    assert ev["store_id"] == "STORE_BLR_002"
    uuid.UUID(ev["event_id"])


def test_build_event_timestamp_correct():
    ev = build_event(
        camera_id="CAM_FLOOR_A", visitor_id="VIS_002",
        event_type="ZONE_ENTER", frame_idx=15, fps=15.0,
        clip_start_utc=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
        zone_id="SKINCARE_TOP", confidence=0.9, session_seq=2,
    )
    assert ev["timestamp"] == "2026-04-10T10:00:01Z"


def test_build_event_unique_ids():
    evs = [
        build_event("CAM_X", "VIS_003", "EXIT", i, 15.0,
                    datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc), confidence=0.8)
        for i in range(50)
    ]
    ids = [e["event_id"] for e in evs]
    assert len(set(ids)) == 50


def test_build_event_metadata_fields():
    ev = build_event(
        camera_id="CAM_BILLING_01", visitor_id="VIS_004",
        event_type="BILLING_QUEUE_JOIN", frame_idx=10, fps=15.0,
        clip_start_utc=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
        queue_depth=3, sku_zone="BILLING", session_seq=5, confidence=0.88,
    )
    assert ev["metadata"]["queue_depth"] == 3
    assert ev["metadata"]["sku_zone"] == "BILLING"
    assert ev["metadata"]["session_seq"] == 5


def test_build_event_confidence_stored():
    ev = build_event(
        camera_id="CAM_ENTRY_01", visitor_id="VIS_005",
        event_type="ENTRY", frame_idx=1, fps=15.0,
        clip_start_utc=datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc),
        confidence=0.72,
    )
    assert ev["confidence"] == pytest.approx(0.72, abs=0.001)


# ── EventEmitter ──────────────────────────────────────────────────────────────

def test_emitter_flush_writes_jsonl(tmp_path):
    em = EventEmitter("CAM_TEST")
    for i in range(3):
        ev = build_event("CAM_TEST", f"VIS_{i}", "ENTRY", i * 10, 15.0,
                         datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc), confidence=0.9)
        em.emit(ev)
    out = str(tmp_path / "test.jsonl")
    em.flush(out)
    lines = open(out).readlines()
    assert len(lines) == 3
    parsed = [json.loads(l) for l in lines]
    assert all("event_id" in p for p in parsed)


def test_emitter_flush_default_path(tmp_path, monkeypatch):
    """flush() with no output_path uses EVENTS_DIR/camera_events.jsonl (line 73)."""
    import pipeline.emit as emit_mod
    monkeypatch.setattr(emit_mod, "EVENTS_DIR", str(tmp_path))
    em = EventEmitter("CAM_DEFAULT")
    em.emit(build_event("CAM_DEFAULT", "VIS_X", "ENTRY", 1, 15.0,
                        datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc), confidence=0.9))
    out = em.flush()
    expected = os.path.join(str(tmp_path), "CAM_DEFAULT_events.jsonl")
    assert out == expected
    assert os.path.exists(out)


def test_emitter_count():
    em = EventEmitter("CAM_TEST")
    assert em.count() == 0
    em.emit({"event_id": "x"})
    assert em.count() == 1


def test_merge_event_files(tmp_path, monkeypatch):
    import pipeline.emit as emit_mod
    monkeypatch.setattr(emit_mod, "EVENTS_DIR", str(tmp_path))
    for cam in ["CAM_A", "CAM_B"]:
        p = tmp_path / f"{cam}_events.jsonl"
        events = [
            build_event(cam, f"VIS_{cam}_{i}", "ENTRY", i, 15.0,
                        datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc), confidence=0.9)
            for i in range(2)
        ]
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    out = str(tmp_path / "all_events.jsonl")
    n = merge_event_files(out)
    assert n == 4
    assert len(open(out).readlines()) == 4


# ── CameraTracker ─────────────────────────────────────────────────────────────

def _make_tracked_detections(bboxes, confs):
    mock_det = MagicMock()
    mock_det.__len__ = lambda self: len(bboxes)
    mock_det.tracker_id = np.array([i + 1 for i in range(len(bboxes))])
    mock_det.xyxy = np.array(bboxes, dtype=np.float32)
    mock_det.confidence = np.array(confs, dtype=np.float32)
    return mock_det


def test_tracker_assigns_visitor_ids():
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)
    det = MagicMock()
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=_make_tracked_detections([[100, 100, 200, 300]], [0.9])):
        updates = tracker.update(det, frame_idx=10)
    assert len(updates) == 1
    tid, state, is_new, conf = updates[0]
    assert is_new is True
    assert state.visitor_id.startswith("VIS_")


def test_tracker_confidence_not_hardcoded():
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)
    det = MagicMock()
    real_conf = 0.63
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=_make_tracked_detections([[100, 100, 200, 300]], [real_conf])):
        updates = tracker.update(det, frame_idx=5)
    _, _, _, conf = updates[0]
    assert conf == pytest.approx(real_conf, abs=0.001)


def test_tracker_no_double_visitor_on_same_track():
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)
    det = MagicMock()
    mock_tracked = _make_tracked_detections([[100, 100, 200, 300]], [0.9])
    mock_tracked.tracker_id = np.array([1])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=mock_tracked):
        u1 = tracker.update(det, 10)
        u2 = tracker.update(det, 20)
    assert u1[0][1].visitor_id == u2[0][1].visitor_id


def test_tracker_lost_tracks_returned():
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)
    state = TrackState(track_id=99, visitor_id="VIS_PLANTED", first_frame=1)
    tracker._tracks[99] = state
    lost = tracker.get_lost_tracks(active_track_ids=set())
    assert any(s.visitor_id == "VIS_PLANTED" for s in lost)
    assert 99 not in tracker._tracks


def test_tracker_get_active_returns_current_tracks():
    """get_active() covers line 134."""
    tracker = CameraTracker("CAM_FLOOR_A", fps=15.0)
    state = TrackState(track_id=1, visitor_id="VIS_ACTIVE", first_frame=1)
    tracker._tracks[1] = state
    active = tracker.get_active()
    assert 1 in active
    assert active[1].visitor_id == "VIS_ACTIVE"


def test_tracker_reentry_detected():
    """Re-entry proximity check — covers lines 61-72, 75, 100-102."""
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)
    det = MagicMock()

    first_det = _make_tracked_detections([[400, 400, 500, 600]], [0.9])
    first_det.tracker_id = np.array([1])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=first_det):
        updates = tracker.update(det, frame_idx=10)
    original_vid = updates[0][1].visitor_id

    lost = tracker.get_lost_tracks(active_track_ids=set())
    assert len(lost) == 1
    lost[0].exit_frame = 20

    new_det = _make_tracked_detections([[410, 410, 510, 610]], [0.88])
    new_det.tracker_id = np.array([2])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=new_det):
        updates2 = tracker.update(det, frame_idx=100)

    _, state2, _, _ = updates2[0]
    assert state2.visitor_id == original_vid
    assert state2.is_reentry is True


def test_tracker_reentry_exit_frame_none_skipped():
    """
    Exited track with exit_frame=None is skipped in proximity check (line 62 continue).
    A second track nearby should get a NEW visitor_id, not reuse the one with None exit_frame.
    """
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)

    # Manually place an exited state with exit_frame=None into _exited pool
    orphan = TrackState(track_id=99, visitor_id="VIS_ORPHAN", first_frame=1)
    orphan.exit_frame = None  # explicitly None — triggers line 62 continue
    orphan.last_bbox = np.array([400.0, 400.0, 500.0, 600.0])
    tracker._exited["VIS_ORPHAN"] = orphan

    det = MagicMock()
    new_det = _make_tracked_detections([[410, 410, 510, 610]], [0.88])
    new_det.tracker_id = np.array([1])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=new_det):
        updates = tracker.update(det, frame_idx=50)

    _, state, _, _ = updates[0]
    # Should NOT reuse VIS_ORPHAN because exit_frame is None → skipped
    assert state.visitor_id != "VIS_ORPHAN"
    assert state.is_reentry is False


def test_tracker_reentry_last_bbox_none_skipped():
    """
    Exited track with last_bbox=None is skipped in proximity check (line 66 continue).
    """
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)

    orphan = TrackState(track_id=98, visitor_id="VIS_NOBBOX", first_frame=1)
    orphan.exit_frame = 10   # valid exit_frame — passes line 62
    orphan.last_bbox = None  # None bbox — triggers line 66 continue
    tracker._exited["VIS_NOBBOX"] = orphan

    det = MagicMock()
    new_det = _make_tracked_detections([[410, 410, 510, 610]], [0.88])
    new_det.tracker_id = np.array([1])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=new_det):
        updates = tracker.update(det, frame_idx=50)

    _, state, _, _ = updates[0]
    assert state.visitor_id != "VIS_NOBBOX"
    assert state.is_reentry is False


def test_tracker_reentry_outside_window_gets_new_id():
    """Track past reentry window → new visitor_id."""
    tracker = CameraTracker("CAM_ENTRY_01", fps=15.0)
    det = MagicMock()

    first_det = _make_tracked_detections([[400, 400, 500, 600]], [0.9])
    first_det.tracker_id = np.array([1])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=first_det):
        updates = tracker.update(det, frame_idx=10)
    original_vid = updates[0][1].visitor_id

    lost = tracker.get_lost_tracks(active_track_ids=set())
    lost[0].exit_frame = 10

    new_det = _make_tracked_detections([[415, 415, 515, 615]], [0.88])
    new_det.tracker_id = np.array([2])
    with patch.object(tracker.byte_tracker, "update_with_detections",
                      return_value=new_det):
        updates2 = tracker.update(det, frame_idx=5000)  # past 300s*15fps=4500 frames

    _, state2, _, _ = updates2[0]
    assert state2.visitor_id != original_vid
    assert state2.is_reentry is False


# ── StaffDetector ─────────────────────────────────────────────────────────────

def test_staff_detector_staff_room_always_staff():
    sd = StaffDetector("CAM_STAFF_01")
    dummy_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    dummy_bbox  = np.array([100, 100, 200, 300], dtype=np.float32)
    assert sd.is_staff(dummy_frame, dummy_bbox) is True


def test_staff_detector_non_billing_not_staff():
    sd = StaffDetector("CAM_FLOOR_A")
    dummy_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    dummy_bbox  = np.array([100, 100, 200, 300], dtype=np.float32)
    assert sd.is_staff(dummy_frame, dummy_bbox) is False


def test_staff_detector_floor_b_not_staff():
    sd = StaffDetector("CAM_FLOOR_B")
    dummy_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    dummy_bbox  = np.array([0, 0, 100, 200], dtype=np.float32)
    assert sd.is_staff(dummy_frame, dummy_bbox) is False


def test_staff_detector_billing_hsv_navy_detected():
    """CAM_BILLING_01 HSV path — navy blue crop triggers is_staff=True (lines 159-169)."""
    import cv2
    sd = StaffDetector("CAM_BILLING_01")
    navy_hsv = np.full((300, 100, 3), [115, 200, 50], dtype=np.uint8)
    navy_bgr = cv2.cvtColor(navy_hsv, cv2.COLOR_HSV2BGR)
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame[0:300, 0:100] = navy_bgr
    bbox = np.array([0.0, 0.0, 100.0, 300.0], dtype=np.float32)
    assert sd.is_staff(frame, bbox) is True


def test_staff_detector_billing_non_uniform_not_staff():
    """CAM_BILLING_01 with black frame — below HSV ratio threshold → not staff."""
    sd = StaffDetector("CAM_BILLING_01")
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    bbox  = np.array([100.0, 100.0, 200.0, 300.0], dtype=np.float32)
    assert sd.is_staff(frame, bbox) is False


def test_staff_detector_billing_zero_size_crop():
    """Empty crop (y1==mid_y) returns False without error (line 163-164)."""
    sd = StaffDetector("CAM_BILLING_01")
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    bbox = np.array([100.0, 100.0, 200.0, 101.0], dtype=np.float32)
    assert sd.is_staff(frame, bbox) is False


# ── detect.py pure helpers ────────────────────────────────────────────────────

def test_detect_centroid():
    from pipeline.detect import centroid
    cx, cy = centroid(np.array([100, 200, 300, 400]))
    assert cx == pytest.approx(200.0)
    assert cy == pytest.approx(300.0)


def test_detect_centroid_zero_bbox():
    from pipeline.detect import centroid
    cx, cy = centroid(np.array([0, 0, 0, 0]))
    assert cx == 0.0 and cy == 0.0


def test_detect_point_in_polygon_inside():
    from pipeline.detect import point_in_polygon
    polygon = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert point_in_polygon(50, 50, polygon) is True


def test_detect_point_in_polygon_outside():
    from pipeline.detect import point_in_polygon
    polygon = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert point_in_polygon(200, 200, polygon) is False


def test_detect_point_in_polygon_on_edge():
    from pipeline.detect import point_in_polygon
    polygon = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert point_in_polygon(0, 50, polygon) is True


def test_detect_line_crossed_entry():
    from pipeline.detect import line_crossed
    assert line_crossed(530.0, 545.0, 540.0) == "entry"


def test_detect_line_crossed_exit():
    from pipeline.detect import line_crossed
    assert line_crossed(550.0, 535.0, 540.0) == "exit"


def test_detect_line_crossed_no_cross():
    from pipeline.detect import line_crossed
    assert line_crossed(520.0, 530.0, 540.0) == ""
    assert line_crossed(550.0, 560.0, 540.0) == ""


def test_detect_load_layout_entry_camera(tmp_path):
    from pipeline.detect import load_layout
    import json
    layout_path = tmp_path / "store_layout.json"
    layout = {
        "zones": [{"zone_id": "ENTRY_LOBBY", "camera_id": "CAM_ENTRY_01",
                   "polygon": [[0,0],[1920,0],[1920,1080],[0,1080]], "sku_zone": "ENTRY"}],
        "entry_line": {"camera_id": "CAM_ENTRY_01", "x1": 200, "y1": 540, "x2": 1720, "y2": 540},
        "dwell_emit_interval_seconds": 30,
        "reentry_window_seconds": 300,
        "billing_queue_min_depth": 1,
    }
    layout_path.write_text(json.dumps(layout))
    import pipeline.detect as detect_mod
    original = detect_mod.LAYOUT_JSON
    detect_mod.LAYOUT_JSON = str(layout_path)
    result = load_layout("CAM_ENTRY_01")
    detect_mod.LAYOUT_JSON = original
    assert result["entry_line"] is not None
    assert result["dwell_interval"] == 30


def test_detect_load_layout_floor_camera_no_entry_line(tmp_path):
    from pipeline.detect import load_layout
    import json
    layout_path = tmp_path / "store_layout.json"
    layout = {
        "zones": [{"zone_id": "SKINCARE_TOP", "camera_id": "CAM_FLOOR_A",
                   "polygon": [[0,0],[1920,0],[1920,400],[0,400]], "sku_zone": "SKINCARE"}],
        "entry_line": {"camera_id": "CAM_ENTRY_01", "x1": 200, "y1": 540, "x2": 1720, "y2": 540},
        "dwell_emit_interval_seconds": 30,
        "reentry_window_seconds": 300,
        "billing_queue_min_depth": 1,
    }
    layout_path.write_text(json.dumps(layout))
    import pipeline.detect as detect_mod
    original = detect_mod.LAYOUT_JSON
    detect_mod.LAYOUT_JSON = str(layout_path)
    result = load_layout("CAM_FLOOR_A")
    detect_mod.LAYOUT_JSON = original
    assert result["entry_line"] is None
    assert len(result["zones"]) == 1