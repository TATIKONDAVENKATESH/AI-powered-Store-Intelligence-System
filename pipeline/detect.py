from __future__ import annotations
"""
Main detection pipeline.
Usage:
  python pipeline/detect.py --store ST1076 --camera CAM3 --video "data/videos/CAM 3 - entry.mp4" --clip-start "2026-03-08T13:00:00"
  python pipeline/detect.py --store ST1008 --camera CAM_ENTRY_1 --video "data/videos/entry 1.mp4" --clip-start "2026-04-10T06:30:00"

Events are written to: data/generated_events/<camera_id>_events.jsonl
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import cv2
import numpy as np
import supervision as sv
from ultralytics import YOLO

# Add project root to sys.path so pipeline package imports work from any CWD
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.tracker import CameraTracker, StaffDetector, ReIDTracker
from pipeline.emit import EventEmitter, build_event, EVENTS_DIR

LAYOUT_JSON = os.getenv("LAYOUT_JSON", "./config/store_layout.json")
YOLO_MODEL  = os.getenv("YOLO_MODEL", "yolov8n.pt")
# 0.25 works better than 0.4 on anonymised (face-blurred) footage
YOLO_CONF   = float(os.getenv("YOLO_CONFIDENCE", "0.25"))

DWELL_EMIT_INTERVAL_S = 30  # emit ZONE_DWELL every 30 seconds of continuous presence


def load_layout(store_id: str) -> dict:
    """Load store layout JSON and return the store-specific section."""
    with open(LAYOUT_JSON) as f:
        layout = json.load(f)
    if store_id not in layout["stores"]:
        raise ValueError(f"Store '{store_id}' not found in {LAYOUT_JSON}. "
                         f"Available: {list(layout['stores'].keys())}")
    return layout["stores"][store_id]


def get_camera_config(store_layout: dict, camera_id: str) -> dict:
    """Return camera config dict; raise clearly if camera_id is unknown."""
    cameras = store_layout.get("cameras", {})
    if camera_id not in cameras:
        raise ValueError(f"Camera '{camera_id}' not in store layout. "
                         f"Available: {list(cameras.keys())}")
    return cameras[camera_id]


def get_zones_for_camera(store_layout: dict, camera_id: str) -> list[dict]:
    """Return zone list for the given camera, or empty list if none defined."""
    return store_layout.get("zones", {}).get(camera_id, [])


def point_in_polygon(point: tuple[float, float], polygon: list[list[int]]) -> bool:
    """Return True if centroid is inside the zone polygon (OpenCV)."""
    pts  = np.array(polygon, dtype=np.float32)
    dist = cv2.pointPolygonTest(pts, point, False)
    return dist >= 0


def process_clip(
    store_id: str,
    camera_id: str,
    video_path: str,
    clip_start_utc: datetime,
) -> str:
    """
    Process one CCTV video clip and write structured JSONL events to disk.

    FIX (root cause of empty generated_events/):
      EventEmitter.flush() writes to EVENTS_DIR/<camera_id>_events.jsonl.
      The previous bug was that EVENTS_DIR defaulted to './data/generated_events'
      but when run.bat cd'd to the project root the path resolved correctly —
      the real issue was that on some runs the video path was wrong (file not
      found), causing cap.read() to return False immediately, so 0 frames were
      processed and flush() wrote an empty file.
      This version: (a) validates video path before processing,
      (b) prints a clear error if the file is missing,
      (c) still calls flush() so the per-camera JSONL is always created.

    Returns:
      Path to the written JSONL file.
    """
    # Validate video file exists before attempting OpenCV open
    if not os.path.exists(video_path):
        print(f"[{camera_id}] ERROR: video file not found: {video_path}")
        print(f"[{camera_id}] Place the .mp4 file at '{video_path}' and re-run.")
        # Write an empty JSONL so merge_event_files() doesn't crash
        emitter = EventEmitter(camera_id)
        output_path = emitter.flush()
        print(f"[{camera_id}] Wrote empty event file: {output_path}")
        return output_path

    store_layout = load_layout(store_id)
    cam_config   = get_camera_config(store_layout, camera_id)
    cam_role     = cam_config["role"]      # "entry" / "zone" / "billing"
    zones        = get_zones_for_camera(store_layout, camera_id)
    entry_line_y = cam_config.get("entry_line_y", 540)

    model = YOLO(YOLO_MODEL)
    cap   = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        print(f"[{camera_id}] ERROR: OpenCV could not open video: {video_path}")
        emitter = EventEmitter(camera_id)
        output_path = emitter.flush()
        return output_path

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[{camera_id}] Processing {video_path} — fps={fps:.1f} frames={total_frames} role={cam_role}")

    tracker   = CameraTracker(store_id, camera_id, fps)
    staff_det = StaffDetector(store_id)
    reid      = ReIDTracker()
    emitter   = EventEmitter(camera_id)

    # Per-track state dictionaries
    prev_centroid_y: dict[int, float] = {}   # entry/exit direction detection
    zone_entry_frame: dict[int, int]  = {}   # track_id → frame entered current zone
    current_zone: dict[int, str]      = {}   # track_id → current zone_id
    last_dwell_frame: dict[int, int]  = {}   # track_id → frame of last ZONE_DWELL emit
    billing_joined: set[int]          = set() # track_ids currently in billing zone
    queue_depth: int                  = 0
    session_seq: dict[int, int]       = {}

    def seq(track_id: int) -> int:
        """Increment and return the per-track event sequence counter."""
        session_seq[track_id] = session_seq.get(track_id, 0) + 1
        return session_seq[track_id]

    frame_idx = 0
    dwell_interval_frames = int(DWELL_EMIT_INTERVAL_S * fps)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # YOLO inference — class 0 = person only, suppress non-person detections
        results    = model(frame, conf=YOLO_CONF, classes=[0], verbose=False)[0]
        detections = sv.Detections.from_ultralytics(results)
        tracked    = tracker.update(detections, frame_idx, frame)

        active_ids: set[int] = set()

        tracker_ids = tracked.tracker_id

        if tracker_ids is None:
            tracker_ids = []

        for i, track_id in enumerate(tracker_ids):
            if track_id is None:
                continue
            active_ids.add(track_id)

            bbox     = tracked.xyxy[i].astype(int)
            conf     = float(tracked.confidence[i]) if tracked.confidence is not None else YOLO_CONF
            cx, cy   = tracker.centroid(bbox)
            is_staff = staff_det.is_staff(frame, tuple(bbox))

            visitor_id, is_reentry = reid.get_visitor_id(track_id, (cx, cy))

            # ── Entry / Exit (entry cameras only) ─────────────────────────
            if cam_role == "entry":
                prev_y = prev_centroid_y.get(track_id)
                if prev_y is not None:
                    crossed_inbound  = prev_y < entry_line_y <= cy
                    crossed_outbound = prev_y >= entry_line_y > cy

                    if crossed_inbound:
                        evt = "REENTRY" if is_reentry else "ENTRY"
                        emitter.emit(build_event(
                            store_id, camera_id, visitor_id, evt,
                            frame_idx, fps, clip_start_utc,
                            confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                        ))

                    elif crossed_outbound:
                        reid.mark_exit(track_id, (cx, cy))
                        emitter.emit(build_event(
                            store_id, camera_id, visitor_id, "EXIT",
                            frame_idx, fps, clip_start_utc,
                            confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                        ))

                prev_centroid_y[track_id] = cy

            # ── Zone detection (zone and billing cameras) ──────────────────
            if cam_role in ("zone", "billing"):
                detected_zone: str | None = None
                for zone in zones:
                    if point_in_polygon((cx, cy), zone["polygon"]):
                        detected_zone = zone["zone_id"]
                        break

                prev_zone = current_zone.get(track_id)

                if detected_zone != prev_zone:
                    # Zone exit
                    if prev_zone:
                        dwell_frames = frame_idx - zone_entry_frame.get(track_id, frame_idx)
                        dwell_ms     = int((dwell_frames / fps) * 1000)

                        emitter.emit(build_event(
                            store_id, camera_id, visitor_id, "ZONE_EXIT",
                            frame_idx, fps, clip_start_utc,
                            zone_id=prev_zone, dwell_ms=dwell_ms,
                            confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                        ))

                        # BILLING_QUEUE_ABANDON: left billing zone; POS correlation
                        # at query time will distinguish converted vs abandoned
                        if "BILLING" in prev_zone and not is_staff and track_id in billing_joined:
                            queue_depth = max(0, queue_depth - 1)
                            billing_joined.discard(track_id)
                            emitter.emit(build_event(
                                store_id, camera_id, visitor_id, "BILLING_QUEUE_ABANDON",
                                frame_idx, fps, clip_start_utc,
                                zone_id=prev_zone, dwell_ms=dwell_ms,
                                confidence=conf, is_staff=is_staff,
                                queue_depth=queue_depth, session_seq=seq(track_id),
                            ))

                    # Zone enter
                    if detected_zone:
                        current_zone[track_id]     = detected_zone
                        zone_entry_frame[track_id] = frame_idx
                        last_dwell_frame[track_id] = frame_idx

                        if "BILLING" in detected_zone and not is_staff:
                            queue_depth += 1
                            billing_joined.add(track_id)
                            emitter.emit(build_event(
                                store_id, camera_id, visitor_id, "BILLING_QUEUE_JOIN",
                                frame_idx, fps, clip_start_utc,
                                zone_id=detected_zone, confidence=conf,
                                is_staff=is_staff, queue_depth=queue_depth,
                                session_seq=seq(track_id),
                            ))
                        else:
                            emitter.emit(build_event(
                                store_id, camera_id, visitor_id, "ZONE_ENTER",
                                frame_idx, fps, clip_start_utc,
                                zone_id=detected_zone, confidence=conf,
                                is_staff=is_staff, session_seq=seq(track_id),
                            ))
                    else:
                        current_zone.pop(track_id, None)

                # ZONE_DWELL — every 30 s of continuous zone presence
                if detected_zone:
                    frames_since_dwell = frame_idx - last_dwell_frame.get(track_id, frame_idx)
                    if frames_since_dwell >= dwell_interval_frames:
                        dwell_ms = int(
                            ((frame_idx - zone_entry_frame.get(track_id, frame_idx)) / fps) * 1000
                        )
                        emitter.emit(build_event(
                            store_id, camera_id, visitor_id, "ZONE_DWELL",
                            frame_idx, fps, clip_start_utc,
                            zone_id=detected_zone, dwell_ms=dwell_ms,
                            confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                        ))
                        last_dwell_frame[track_id] = frame_idx

        # Emit ZONE_EXIT for tracks that vanished (left frame without crossing a line)
        for track_id in list(current_zone.keys()):
            if track_id not in active_ids:
                zone_id = current_zone.pop(track_id)
                # FIX: guard against track_id with no known centroid
                last_centroid = reid.get_last_centroid(track_id) or (0.0, 0.0)
                visitor_id, _ = reid.get_visitor_id(track_id, last_centroid)
                dwell_frames  = frame_idx - zone_entry_frame.get(track_id, frame_idx)
                dwell_ms      = int((dwell_frames / fps) * 1000)
                emitter.emit(build_event(
                    store_id, camera_id, visitor_id, "ZONE_EXIT",
                    frame_idx, fps, clip_start_utc,
                    zone_id=zone_id, dwell_ms=dwell_ms,
                    confidence=YOLO_CONF, session_seq=seq(track_id),
                ))

        frame_idx += 1

    cap.release()
    output_path = emitter.flush()  # always writes, even if 0 events
    print(f"[{camera_id}] {emitter.count()} events written → {output_path}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process one CCTV clip and emit events to JSONL")
    parser.add_argument("--store",      required=True, help="Store ID: ST1076 or ST1008")
    parser.add_argument("--camera",     required=True, help="Camera ID from store_layout.json")
    parser.add_argument("--video",      required=True, help="Path to .mp4 file")
    parser.add_argument("--clip-start", default=None,
                        help="Clip start UTC in ISO format e.g. 2026-03-08T13:00:00 (default: now)")
    args = parser.parse_args()

    if args.clip_start:
        clip_start = datetime.fromisoformat(args.clip_start).replace(tzinfo=timezone.utc)
    else:
        clip_start = datetime.now(timezone.utc)

    process_clip(args.store, args.camera, args.video, clip_start)