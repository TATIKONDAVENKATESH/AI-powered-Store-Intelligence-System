from __future__ import annotations
"""
Main detection pipeline.
Usage:
  python pipeline/detect.py --store ST1076 --camera CAM3 --video "data/videos/CAM 3 - entry.mp4"
  python pipeline/detect.py --store ST1008 --camera CAM_ENTRY_1 --video "data/videos/entry 1.mp4"
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

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.tracker import CameraTracker, StaffDetector
from pipeline.emit import EventEmitter, build_event, EVENTS_DIR

# Config paths
LAYOUT_JSON   = os.getenv("LAYOUT_JSON", "./config/store_layout.json")
YOLO_MODEL    = os.getenv("YOLO_MODEL", "yolov8n.pt")
YOLO_CONF     = float(os.getenv("YOLO_CONFIDENCE", "0.4"))

# Zone dwell emit interval (30 seconds × fps converted per-frame)
DWELL_EMIT_INTERVAL_S = 30


def load_layout(store_id: str) -> dict:
    with open(LAYOUT_JSON) as f:
        layout = json.load(f)
    return layout["stores"][store_id]


def get_camera_config(store_layout: dict, camera_id: str) -> dict:
    return store_layout["cameras"][camera_id]


def get_zones_for_camera(store_layout: dict, camera_id: str) -> list[dict]:
    return store_layout.get("zones", {}).get(camera_id, [])


def point_in_polygon(point: tuple[float, float], polygon: list[list[int]]) -> bool:
    """Check if a point is inside a polygon using OpenCV."""
    pts  = np.array(polygon, dtype=np.float32)
    dist = cv2.pointPolygonTest(pts, point, False)
    return dist >= 0


def process_clip(
    store_id: str,
    camera_id: str,
    video_path: str,
    clip_start_utc: datetime,
) -> str:
    """Process one video clip and write JSONL events. Returns output path."""
    store_layout   = load_layout(store_id)
    cam_config     = get_camera_config(store_layout, camera_id)
    cam_role       = cam_config["role"]               # entry / zone / billing
    zones          = get_zones_for_camera(store_layout, camera_id)
    entry_line_y   = cam_config.get("entry_line_y", 540)

    model          = YOLO(YOLO_MODEL)
    cap            = cv2.VideoCapture(video_path)
    fps            = cap.get(cv2.CAP_PROP_FPS) or 15.0
    tracker        = CameraTracker(store_id, camera_id, fps)
    staff_det      = StaffDetector(store_id)
    emitter        = EventEmitter(camera_id)

    # Re-ID and session state
    from pipeline.tracker import ReIDTracker
    reid = ReIDTracker()

    # Per-track state for this clip
    prev_centroid_y: dict[int, float]  = {}  # for entry/exit direction detection
    zone_entry_frame: dict[int, int]   = {}  # track_id → frame entered current zone
    current_zone: dict[int, str]        = {}  # track_id → current zone_id
    last_dwell_frame: dict[int, int]    = {}  # track_id → frame of last ZONE_DWELL emit
    has_entered: set[int]               = set()  # tracks that crossed entry inbound
    billing_join_frame: dict[int, int]  = {}   # track_id → frame entered billing
    queue_depth: int                    = 0
    session_seq: dict[int, int]         = {}

    def seq(track_id: int) -> int:
        session_seq[track_id] = session_seq.get(track_id, 0) + 1
        return session_seq[track_id]

    frame_idx = 0
    dwell_interval_frames = int(DWELL_EMIT_INTERVAL_S * fps)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # YOLO inference — detect persons only (class 0)
        results     = model(frame, conf=YOLO_CONF, classes=[0], verbose=False)[0]
        detections  = sv.Detections.from_ultralytics(results)
        tracked     = tracker.update(detections, frame_idx, frame)

        active_ids: set[int] = set()

        for i, track_id in enumerate(tracked.tracker_id or []):
            if track_id is None:
                continue
            active_ids.add(track_id)

            bbox       = tracked.xyxy[i].astype(int)
            conf       = float(tracked.confidence[i]) if tracked.confidence is not None else YOLO_CONF
            cx, cy     = tracker.centroid(bbox)
            is_staff   = staff_det.is_staff(frame, tuple(bbox))

            visitor_id, is_reentry = reid.get_visitor_id(track_id, (cx, cy))

            # --- Entry / Exit (entry cameras only) ---
            if cam_role == "entry":
                prev_y = prev_centroid_y.get(track_id)
                if prev_y is not None:
                    if prev_y < entry_line_y and cy >= entry_line_y:
                        # Crossed line downward = ENTRY (into store)
                        if is_reentry:
                            emitter.emit(build_event(
                                camera_id, visitor_id, "REENTRY", frame_idx, fps,
                                clip_start_utc, confidence=conf, is_staff=is_staff,
                                session_seq=seq(track_id),
                            ))
                        else:
                            has_entered.add(track_id)
                            emitter.emit(build_event(
                                camera_id, visitor_id, "ENTRY", frame_idx, fps,
                                clip_start_utc, confidence=conf, is_staff=is_staff,
                                session_seq=seq(track_id),
                            ))
                    elif prev_y >= entry_line_y and cy < entry_line_y:
                        # Crossed line upward = EXIT (out of store)
                        reid.mark_exit(track_id, (cx, cy))
                        emitter.emit(build_event(
                            camera_id, visitor_id, "EXIT", frame_idx, fps,
                            clip_start_utc, confidence=conf, is_staff=is_staff,
                            session_seq=seq(track_id),
                        ))
                prev_centroid_y[track_id] = cy

            # --- Zone detection (zone and billing cameras) ---
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
                            camera_id, visitor_id, "ZONE_EXIT", frame_idx, fps,
                            clip_start_utc, zone_id=prev_zone, dwell_ms=dwell_ms,
                            confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                        ))
                        # Billing queue abandon — left billing without POS match
                        if "BILLING" in prev_zone and not is_staff:
                            queue_depth = max(0, queue_depth - 1)
                            emitter.emit(build_event(
                                camera_id, visitor_id, "BILLING_QUEUE_ABANDON", frame_idx, fps,
                                clip_start_utc, zone_id=prev_zone, dwell_ms=dwell_ms,
                                confidence=conf, is_staff=is_staff,
                                queue_depth=queue_depth, session_seq=seq(track_id),
                            ))

                    # Zone enter
                    if detected_zone:
                        current_zone[track_id]      = detected_zone
                        zone_entry_frame[track_id]  = frame_idx
                        last_dwell_frame[track_id]  = frame_idx

                        if "BILLING" in detected_zone and not is_staff:
                            queue_depth += 1
                            emitter.emit(build_event(
                                camera_id, visitor_id, "BILLING_QUEUE_JOIN", frame_idx, fps,
                                clip_start_utc, zone_id=detected_zone,
                                confidence=conf, is_staff=is_staff,
                                queue_depth=queue_depth, session_seq=seq(track_id),
                            ))
                        else:
                            emitter.emit(build_event(
                                camera_id, visitor_id, "ZONE_ENTER", frame_idx, fps,
                                clip_start_utc, zone_id=detected_zone,
                                confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                            ))
                    else:
                        current_zone.pop(track_id, None)

                # ZONE_DWELL — emit every 30s of continuous presence
                if detected_zone and (frame_idx - last_dwell_frame.get(track_id, frame_idx)) >= dwell_interval_frames:
                    dwell_ms = int(((frame_idx - zone_entry_frame.get(track_id, frame_idx)) / fps) * 1000)
                    emitter.emit(build_event(
                        camera_id, visitor_id, "ZONE_DWELL", frame_idx, fps,
                        clip_start_utc, zone_id=detected_zone, dwell_ms=dwell_ms,
                        confidence=conf, is_staff=is_staff, session_seq=seq(track_id),
                    ))
                    last_dwell_frame[track_id] = frame_idx

        # Emit EXIT for tracks that vanished (left frame without crossing line on zone cams)
        for track_id in list(current_zone.keys()):
            if track_id not in active_ids:
                zone_id = current_zone.pop(track_id)
                visitor_id, _ = reid.get_visitor_id(track_id, (0, 0))
                dwell_frames  = frame_idx - zone_entry_frame.get(track_id, frame_idx)
                dwell_ms      = int((dwell_frames / fps) * 1000)
                emitter.emit(build_event(
                    camera_id, visitor_id, "ZONE_EXIT", frame_idx, fps,
                    clip_start_utc, zone_id=zone_id, dwell_ms=dwell_ms,
                    confidence=YOLO_CONF, session_seq=seq(track_id),
                ))

        frame_idx += 1

    cap.release()
    output_path = emitter.flush()
    print(f"[{camera_id}] {emitter.count()} events → {output_path}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process one CCTV clip")
    parser.add_argument("--store",  required=True, help="Store ID: ST1076 or ST1008")
    parser.add_argument("--camera", required=True, help="Camera ID from store_layout.json")
    parser.add_argument("--video",  required=True, help="Path to .mp4 file")
    parser.add_argument(
        "--clip-start",
        default=None,
        help="Clip start datetime UTC in ISO format (default: now)",
    )
    args = parser.parse_args()

    os.environ["STORE_ID"] = args.store

    if args.clip_start:
        clip_start = datetime.fromisoformat(args.clip_start).replace(tzinfo=timezone.utc)
    else:
        clip_start = datetime.now(timezone.utc)

    process_clip(args.store, args.camera, args.video, clip_start)