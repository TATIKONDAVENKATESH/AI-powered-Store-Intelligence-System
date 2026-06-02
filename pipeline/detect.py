from __future__ import annotations
"""
detect.py — Process one CCTV video file and emit structured events.

Usage:
    python pipeline/detect.py --camera CAM_ENTRY_01 --video data/videos/entry_camera.mp4
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import cv2
import numpy as np
from ultralytics import YOLO
import supervision as sv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.tracker import CameraTracker, StaffDetector
from pipeline.emit import EventEmitter, build_event

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LAYOUT_JSON = os.getenv("LAYOUT_JSON", "./config/store_layout.json")
YOLO_MODEL  = os.getenv("YOLO_MODEL", "models/yolov8n.pt")
YOLO_CONF   = float(os.getenv("YOLO_CONFIDENCE", "0.4"))

# Clip anchor: treat frame 0 as this UTC wall-clock time (Brigade Bangalore dataset date)
CLIP_START_UTC = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)


def load_layout(camera_id: str) -> dict:
    """Return zones, entry-line config, and timing params for this camera."""
    with open(LAYOUT_JSON) as f:
        layout = json.load(f)
    camera_zones = [z for z in layout["zones"] if z["camera_id"] == camera_id]
    entry_line = layout.get("entry_line")
    if entry_line and entry_line.get("camera_id") != camera_id:
        entry_line = None
    return {
        "zones": camera_zones,
        "entry_line": entry_line,
        "dwell_interval": layout.get("dwell_emit_interval_seconds", 30),
        "reentry_window": layout.get("reentry_window_seconds", 300),
        "billing_min_depth": layout.get("billing_queue_min_depth", 1),
    }


def centroid(bbox: np.ndarray) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def point_in_polygon(px: float, py: float, polygon: list) -> bool:
    pts = np.array(polygon, dtype=np.float32)
    return cv2.pointPolygonTest(pts, (float(px), float(py)), False) >= 0


def line_crossed(prev_y: float, curr_y: float, line_y: float) -> str:
    """Returns 'entry', 'exit', or '' based on vertical crossing direction."""
    if prev_y < line_y <= curr_y:
        return "entry"
    if prev_y > line_y >= curr_y:
        return "exit"
    return ""


def process_video(camera_id: str, video_path: str) -> int:
    layout    = load_layout(camera_id)
    zones     = layout["zones"]
    entry_cfg = layout["entry_line"]
    dwell_s   = layout["dwell_interval"]

    logger.info("Loading YOLO: %s", YOLO_MODEL)
    model = YOLO(YOLO_MODEL)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")

    fps          = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dwell_frames = int(dwell_s * fps)

    logger.info("Camera=%s  fps=%.1f  frames=%d", camera_id, fps, total_frames)

    tracker        = CameraTracker(camera_id=camera_id, fps=fps, frame_rate=int(fps))
    staff_detector = StaffDetector(camera_id=camera_id)
    emitter        = EventEmitter(camera_id=camera_id)

    prev_cy_map: dict[str, float] = {}
    entered:      set[str]        = set()
    exited:       set[str]        = set()
    billing_queue: set[str]       = set()

    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1

        results    = model(frame, classes=[0], conf=YOLO_CONF, verbose=False)[0]
        detections = sv.Detections.from_ultralytics(results)

        if len(detections) == 0:
            # Flush lost tracks as exits when no detections in frame
            for state in tracker.get_lost_tracks(set()):
                if state.visitor_id not in exited:
                    _do_exit(state, camera_id, frame_idx, fps, emitter, exited)
            continue

        # tracker.update returns (tid, state, is_new, conf) — conf from ByteTrack
        tracked     = tracker.update(detections, frame_idx)
        active_ids  = {tid for tid, _, _, _ in tracked}
        lost_tracks = tracker.get_lost_tracks(active_ids)

        for state in lost_tracks:
            if state.visitor_id not in exited:
                _do_exit(state, camera_id, frame_idx, fps, emitter, exited)

        for tid, state, is_new, conf in tracked:  # conf unpacked from tracker, not hardcoded
            bbox   = state.last_bbox
            cx, cy = centroid(bbox)
            is_stf = staff_detector.is_staff(frame, bbox)

            # Entry/Exit line crossing — only on CAM_ENTRY_01
            if entry_cfg is not None:
                line_y  = float(entry_cfg["y1"])
                prev_cy = prev_cy_map.get(state.visitor_id, cy)
                cross   = line_crossed(prev_cy, cy, line_y)

                if cross == "entry" and state.visitor_id not in entered:
                    ev_type = "REENTRY" if state.is_reentry else "ENTRY"
                    state.session_seq += 1
                    emitter.emit(build_event(
                        camera_id=camera_id, visitor_id=state.visitor_id,
                        event_type=ev_type, frame_idx=frame_idx, fps=fps,
                        clip_start_utc=CLIP_START_UTC, is_staff=is_stf,
                        confidence=conf, session_seq=state.session_seq,
                    ))
                    entered.add(state.visitor_id)

                elif cross == "exit" and state.visitor_id not in exited:
                    _do_exit(state, camera_id, frame_idx, fps, emitter, exited,
                             is_staff=is_stf, conf=conf)

            # Zone logic — floor and billing cameras
            _handle_zones(
                state=state, camera_id=camera_id, cx=cx, cy=cy,
                frame_idx=frame_idx, fps=fps, zones=zones,
                is_staff=is_stf, conf=conf, emitter=emitter,
                dwell_frames=dwell_frames, billing_queue=billing_queue,
            )

            prev_cy_map[state.visitor_id] = cy

        if frame_idx % 300 == 0:
            logger.info("  frame %d/%d  events=%d", frame_idx, total_frames, emitter.count())

    cap.release()

    # Flush remaining active tracks as exits at video end
    for state in tracker.get_active().values():
        if state.visitor_id not in exited:
            _do_exit(state, camera_id, frame_idx, fps, emitter, exited)

    out = emitter.flush()
    logger.info("Done camera=%s  events=%d  file=%s", camera_id, emitter.count(), out)
    return emitter.count()


def _do_exit(state, camera_id, frame_idx, fps, emitter, exited_set,
             is_staff=False, conf=0.85):
    """Emit EXIT event and mark visitor as exited."""
    state.session_seq += 1
    emitter.emit(build_event(
        camera_id=camera_id, visitor_id=state.visitor_id,
        event_type="EXIT", frame_idx=frame_idx, fps=fps,
        clip_start_utc=CLIP_START_UTC, is_staff=is_staff,
        confidence=conf, session_seq=state.session_seq,
    ))
    exited_set.add(state.visitor_id)
    state.exited     = True
    state.exit_frame = frame_idx


def _handle_zones(state, camera_id, cx, cy, frame_idx, fps, zones,
                  is_staff, conf, emitter, dwell_frames, billing_queue):
    """Emit ZONE_ENTER / ZONE_EXIT / ZONE_DWELL / BILLING_QUEUE_JOIN / ABANDON."""
    current_zone = None
    current_sku  = None
    for zone in zones:
        if point_in_polygon(cx, cy, zone["polygon"]):
            current_zone = zone["zone_id"]
            current_sku  = zone.get("sku_zone")
            break

    prev_zone = state.zone_id

    if current_zone != prev_zone:
        if prev_zone is not None:
            dwell_ms = int((frame_idx - (state.zone_enter_frame or frame_idx)) / fps * 1000)
            state.session_seq += 1
            emitter.emit(build_event(
                camera_id=camera_id, visitor_id=state.visitor_id,
                event_type="ZONE_EXIT", frame_idx=frame_idx, fps=fps,
                clip_start_utc=CLIP_START_UTC, zone_id=prev_zone,
                dwell_ms=dwell_ms, is_staff=is_staff, confidence=conf,
                session_seq=state.session_seq,
            ))
            # Leaving billing without a purchase = abandon
            if prev_zone == "BILLING" and state.visitor_id in billing_queue:
                state.session_seq += 1
                emitter.emit(build_event(
                    camera_id=camera_id, visitor_id=state.visitor_id,
                    event_type="BILLING_QUEUE_ABANDON", frame_idx=frame_idx, fps=fps,
                    clip_start_utc=CLIP_START_UTC, zone_id="BILLING",
                    is_staff=is_staff, confidence=conf,
                    session_seq=state.session_seq,
                ))
                billing_queue.discard(state.visitor_id)

        if current_zone is not None:
            state.session_seq += 1
            q_depth = len(billing_queue) if current_zone == "BILLING" else None
            emitter.emit(build_event(
                camera_id=camera_id, visitor_id=state.visitor_id,
                event_type="ZONE_ENTER", frame_idx=frame_idx, fps=fps,
                clip_start_utc=CLIP_START_UTC, zone_id=current_zone,
                is_staff=is_staff, confidence=conf, sku_zone=current_sku,
                queue_depth=q_depth, session_seq=state.session_seq,
            ))
            if current_zone == "BILLING" and not is_staff:
                billing_queue.add(state.visitor_id)
                state.session_seq += 1
                emitter.emit(build_event(
                    camera_id=camera_id, visitor_id=state.visitor_id,
                    event_type="BILLING_QUEUE_JOIN", frame_idx=frame_idx, fps=fps,
                    clip_start_utc=CLIP_START_UTC, zone_id="BILLING",
                    is_staff=is_staff, confidence=conf,
                    queue_depth=len(billing_queue), session_seq=state.session_seq,
                ))

        state.zone_id           = current_zone
        state.zone_enter_frame  = frame_idx
        state.dwell_emitted_frame = frame_idx

    elif current_zone is not None:
        since = frame_idx - (state.dwell_emitted_frame or frame_idx)
        if since >= dwell_frames:
            dwell_ms = int(since / fps * 1000)
            state.session_seq += 1
            emitter.emit(build_event(
                camera_id=camera_id, visitor_id=state.visitor_id,
                event_type="ZONE_DWELL", frame_idx=frame_idx, fps=fps,
                clip_start_utc=CLIP_START_UTC, zone_id=current_zone,
                dwell_ms=dwell_ms, is_staff=is_staff, confidence=conf,
                sku_zone=current_sku, session_seq=state.session_seq,
            ))
            state.dwell_emitted_frame = frame_idx


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", required=True)
    parser.add_argument("--video",  required=True)
    args = parser.parse_args()
    process_video(camera_id=args.camera, video_path=args.video)
