from __future__ import annotations
"""
Tracking, Re-ID, and staff detection for one camera clip.
"""
import json
import os
import time
from typing import Optional
import numpy as np

_LAYOUT_PATH  = os.getenv("LAYOUT_JSON", "./config/store_layout.json")
_LAYOUT_CACHE: dict | None = None


def _get_layout() -> dict:
    """Load layout JSON once and cache it."""
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is None:
        # Resolve path relative to project root if relative
        path = _LAYOUT_PATH
        if not os.path.isabs(path):
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.normpath(os.path.join(project_root, path))
        with open(path) as f:
            _LAYOUT_CACHE = json.load(f)
    return _LAYOUT_CACHE


def _get_staff_hsv(store_id: str) -> tuple[list, list]:
    """Return HSV lower/upper bounds for staff uniform detection from layout."""
    store = _get_layout()["stores"].get(store_id, {})
    hsv   = store.get("staff_uniform_hsv", {"lower": [0, 0, 0], "upper": [180, 60, 80]})
    return hsv["lower"], hsv["upper"]


class StaffDetector:
    """Detects staff by uniform colour using HSV range configured per store."""

    def __init__(self, store_id: str):
        self.lower, self.upper = _get_staff_hsv(store_id)
        self._lower_np = np.array(self.lower, dtype=np.uint8)
        self._upper_np = np.array(self.upper, dtype=np.uint8)

    def is_staff(self, frame: np.ndarray, bbox: tuple[int, int, int, int]) -> bool:
        """Return True if >25% of bbox pixels match the staff uniform HSV range."""
        import cv2
        x1, y1, x2, y2 = bbox
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            return False
        crop  = frame[y1:y2, x1:x2]
        hsv   = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask  = cv2.inRange(hsv, self._lower_np, self._upper_np)
        ratio = np.count_nonzero(mask) / (mask.size or 1)
        return ratio > 0.25


class ReIDTracker:
    """
    Centroid-based Re-ID: maps ByteTrack integer track_id → stable visitor_id string.

    When a new track appears near the entry line, we check the recently-exited pool.
    If a match is found (centroid within REENTRY_DIST_PX, within REENTRY_WINDOW_S),
    we reuse the existing visitor_id and flag is_reentry=True.
    """

    REENTRY_WINDOW_S = 300   # 5-minute re-entry window
    REENTRY_DIST_PX  = 200   # max centroid distance for same-person match

    def __init__(self):
        self._track_to_visitor: dict[int, str]       = {}
        self._track_last_centroid: dict[int, tuple]  = {}  # track_id → (cx, cy)
        self._exited: list[dict]                     = []  # {visitor_id, centroid, exited_at}
        self._seq: int                               = 0   # monotonic counter for unique IDs

    def _next_id(self) -> str:
        """Generate a new unique visitor ID."""
        self._seq += 1
        return f"VIS_{self._seq:04d}"

    def get_visitor_id(
        self, track_id: int, centroid: tuple[float, float]
    ) -> tuple[str, bool]:
        """
        Return (visitor_id, is_reentry).
        On first sight: check exited pool for re-entry match; else assign new ID.
        """
        # Always update last-known centroid for this track
        self._track_last_centroid[track_id] = centroid

        if track_id in self._track_to_visitor:
            return self._track_to_visitor[track_id], False

        # Search recently-exited visitors for re-entry match
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
        visitor_id = self._next_id()
        self._track_to_visitor[track_id] = visitor_id
        return visitor_id, False

    def get_last_centroid(self, track_id: int) -> Optional[tuple[float, float]]:
        """
        Return last known centroid for a track.

        FIX: Returns None if track_id was never seen (prevents KeyError crash in
        detect.py's vanished-track handler).
        """
        return self._track_last_centroid.get(track_id)

    def mark_exit(self, track_id: int, centroid: tuple[float, float]) -> None:
        """Record exit so we can detect re-entry within the time window."""
        visitor_id = self._track_to_visitor.get(track_id)
        if visitor_id:
            self._exited.append({
                "visitor_id": visitor_id,
                "centroid":   centroid,
                "exited_at":  time.time(),
            })
            # Evict stale records to bound memory usage
            cutoff       = time.time() - self.REENTRY_WINDOW_S
            self._exited = [r for r in self._exited if r["exited_at"] > cutoff]


class CameraTracker:
    """
    Wraps supervision ByteTrack for one camera.
    Exposes update() and centroid() only — state management lives in detect.py.
    """

    def __init__(self, store_id: str, camera_id: str, fps: float = 15.0):
        import supervision as sv
        self.store_id  = store_id
        self.camera_id = camera_id
        self.fps       = fps
        # 3-second lost-track buffer handles brief occlusions behind displays
        self.tracker   = sv.ByteTrack(lost_track_buffer=int(fps * 3))

    def update(self, detections, frame_idx: int, frame: np.ndarray):
        """Feed YOLO detections into ByteTrack. Returns sv.Detections with tracker_id."""
        return self.tracker.update_with_detections(detections)

    def centroid(self, bbox) -> tuple[float, float]:
        """Compute bounding-box centroid from xyxy array."""
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)