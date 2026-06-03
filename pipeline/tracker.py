from __future__ import annotations
import json
import os
import time
from collections import defaultdict
from typing import Optional
import numpy as np

# Load store layout once for HSV staff params
_LAYOUT_PATH = os.getenv("LAYOUT_JSON", "./config/store_layout.json")
with open(_LAYOUT_PATH) as f:
    _LAYOUT = json.load(f)


def _get_staff_hsv(store_id: str) -> tuple[list, list]:
    """Return HSV lower/upper bounds for staff uniform detection."""
    store = _LAYOUT["stores"].get(store_id, {})
    hsv = store.get("staff_uniform_hsv", {"lower": [0, 0, 0], "upper": [180, 60, 80]})
    return hsv["lower"], hsv["upper"]


class StaffDetector:
    """Detects staff by uniform colour in the billing area frame."""

    def __init__(self, store_id: str):
        self.lower, self.upper = _get_staff_hsv(store_id)
        self._lower_np = np.array(self.lower, dtype=np.uint8)
        self._upper_np = np.array(self.upper, dtype=np.uint8)

    def is_staff(self, frame: np.ndarray, bbox: tuple[int, int, int, int]) -> bool:
        """Check if the dominant colour of a bounding box matches staff uniform."""
        import cv2
        x1, y1, x2, y2 = bbox
        # Clamp to frame bounds
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            return False
        crop = frame[y1:y2, x1:x2]
        hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self._lower_np, self._upper_np)
        # Staff if > 25% of crop pixels match uniform colour
        ratio = np.count_nonzero(mask) / (mask.size or 1)
        return ratio > 0.25


class ReIDTracker:
    """
    Simple centroid-based Re-ID.
    Maps numeric track_id → visitor_id string. Re-uses visitor_id if the centroid
    re-appears within REENTRY_WINDOW_SECONDS after an EXIT.
    """

    REENTRY_WINDOW_S = 300   # 5-minute window for re-entry detection
    REENTRY_DIST_PX  = 200   # max centroid distance to consider same person

    def __init__(self):
        self._track_to_visitor: dict[int, str]   = {}
        self._exited: list[dict]                  = []  # {visitor_id, centroid, exited_at}
        self._visitor_seq: dict[str, int]         = defaultdict(int)

    def get_visitor_id(
        self, track_id: int, centroid: tuple[float, float]
    ) -> tuple[str, bool]:
        """
        Return (visitor_id, is_reentry).
        Assigns a new visitor_id on first sight, or re-uses one from exited list.
        """
        if track_id in self._track_to_visitor:
            return self._track_to_visitor[track_id], False

        # Check exited visitors for Re-ID
        now = time.time()
        for record in self._exited:
            if now - record["exited_at"] > self.REENTRY_WINDOW_S:
                continue
            cx, cy = record["centroid"]
            dist   = ((centroid[0] - cx) ** 2 + (centroid[1] - cy) ** 2) ** 0.5
            if dist < self.REENTRY_DIST_PX:
                visitor_id = record["visitor_id"]
                self._track_to_visitor[track_id] = visitor_id
                return visitor_id, True  # re-entry detected

        # New visitor
        visitor_id = f"VIS_{track_id:04d}"
        self._track_to_visitor[track_id] = visitor_id
        return visitor_id, False

    def mark_exit(self, track_id: int, centroid: tuple[float, float]) -> None:
        """Record exit so we can detect re-entry."""
        visitor_id = self._track_to_visitor.get(track_id)
        if visitor_id:
            self._exited.append({
                "visitor_id": visitor_id,
                "centroid":   centroid,
                "exited_at":  time.time(),
            })
            # Remove stale records to save memory
            cutoff = time.time() - self.REENTRY_WINDOW_S
            self._exited = [r for r in self._exited if r["exited_at"] > cutoff]

    def next_session_seq(self, visitor_id: str) -> int:
        """Increment and return the event sequence number for a visitor session."""
        self._visitor_seq[visitor_id] += 1
        return self._visitor_seq[visitor_id]


class CameraTracker:
    """
    Wraps supervision ByteTrack + ReIDTracker for a single camera.
    Stores per-track state: current zone, zone entry time, dwell accumulators.
    """

    def __init__(self, store_id: str, camera_id: str, fps: float = 15.0):
        import supervision as sv
        self.store_id  = store_id
        self.camera_id = camera_id
        self.fps       = fps
        self.tracker   = sv.ByteTrack(lost_track_buffer=int(fps * 3))  # 3s buffer
        self.reid      = ReIDTracker()
        self.staff_det = StaffDetector(store_id)

        # Per-track state
        self._zone_entry_frame: dict[int, int]  = {}   # track_id → frame when entered zone
        self._current_zone: dict[int, str]       = {}   # track_id → zone_id
        self._last_dwell_frame: dict[int, int]   = {}   # track_id → last ZONE_DWELL emit frame
        self._crossed_entry: set[int]            = set()  # tracks that crossed entry line

    def update(
        self, detections, frame_idx: int, frame: np.ndarray
    ):
        """
        Feed YOLO detections into ByteTrack. Returns supervision Detections with track_ids.
        """
        return self.tracker.update_with_detections(detections)

    def centroid(self, bbox) -> tuple[float, float]:
        """Compute centroid from xyxy bounding box."""
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2, (y1 + y2) / 2)