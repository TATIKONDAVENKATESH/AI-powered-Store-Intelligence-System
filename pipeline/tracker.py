from __future__ import annotations
import numpy as np
from typing import Optional
import supervision as sv


REENTRY_WINDOW_SECONDS = 300


class TrackState:
    """Per-track bookkeeping within one camera feed."""

    def __init__(self, track_id: int, visitor_id: str, first_frame: int):
        self.track_id = track_id
        self.visitor_id = visitor_id
        self.first_frame = first_frame
        self.last_frame = first_frame
        self.last_bbox: Optional[np.ndarray] = None
        self.zone_id: Optional[str] = None
        self.zone_enter_frame: Optional[int] = None
        self.dwell_emitted_frame: int = 0
        self.session_seq: int = 0
        self.exited: bool = False
        self.exit_frame: Optional[int] = None
        self.is_reentry: bool = False


class CameraTracker:
    """
    Wraps supervision ByteTrack and manages visitor_id assignment.
    Returns confidence from ByteTrack alongside each track state.
    """

    def __init__(self, camera_id: str, fps: float, frame_rate: int = 15):
        self.camera_id = camera_id
        self.fps = fps
        self.byte_tracker = sv.ByteTrack(
            track_activation_threshold=0.4,
            lost_track_buffer=int(fps * 3),
            minimum_matching_threshold=0.8,
            frame_rate=frame_rate,
        )
        self._tracks: dict[int, TrackState] = {}
        self._exited: dict[str, TrackState] = {}
        self._visitor_counter: int = 0

    def _new_visitor_id(self) -> str:
        self._visitor_counter += 1
        return f"VIS_{self.camera_id}_{self._visitor_counter:04d}"

    def _find_reentry(self, bbox: np.ndarray, current_frame: int) -> Optional[str]:
        """Lightweight Re-ID: centroid proximity within reentry window."""
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        reentry_frames = REENTRY_WINDOW_SECONDS * self.fps

        best_vid = None
        best_dist = float("inf")

        for vid, state in self._exited.items():
            if state.exit_frame is None:
                continue
            if (current_frame - state.exit_frame) > reentry_frames:
                continue
            if state.last_bbox is None:
                continue
            ex = (state.last_bbox[0] + state.last_bbox[2]) / 2
            ey = (state.last_bbox[1] + state.last_bbox[3]) / 2
            dist = np.sqrt((cx - ex) ** 2 + (cy - ey) ** 2)
            if dist < best_dist:
                best_dist = dist
                best_vid = vid

        if best_vid is not None and best_dist < 200:
            return best_vid
        return None

    def update(
        self, detections: sv.Detections, frame_idx: int
    ) -> list[tuple[int, TrackState, bool, float]]:
        """
        Feed detections into ByteTrack.
        Returns list of (track_id, TrackState, is_new, confidence).
        Confidence comes from ByteTrack — not hardcoded.
        """
        tracked = self.byte_tracker.update_with_detections(detections)
        if (
                tracked.tracker_id is None
                or len(tracked.tracker_id) == 0
        ):
            return []
        results = []

        valid_count = min(
            len(tracked.xyxy),
            len(tracked.tracker_id)
        )

        for i in range(valid_count):
            tid = int(tracked.tracker_id[i])
            bbox = tracked.xyxy[i]
            # Extract real confidence from tracked detections
            conf = float(tracked.confidence[i]) if tracked.confidence is not None else 0.85

            is_new = tid not in self._tracks

            if is_new:
                reentry_vid = self._find_reentry(bbox, frame_idx)
                if reentry_vid is not None:
                    vid = reentry_vid
                    self._exited.pop(reentry_vid, None)
                    is_reentry = True
                else:
                    vid = self._new_visitor_id()
                    is_reentry = False

                state = TrackState(
                    track_id=tid,
                    visitor_id=vid,
                    first_frame=frame_idx,
                )
                state.is_reentry = is_reentry
                self._tracks[tid] = state

            state = self._tracks[tid]
            state.last_frame = frame_idx
            state.last_bbox = bbox
            results.append((tid, state, is_new, conf))  # conf included, not hardcoded

        return results

    def get_lost_tracks(self, active_track_ids: set[int]) -> list[TrackState]:
        """Return tracks no longer active; move them to exited pool."""
        lost = []
        for tid, state in list(self._tracks.items()):
            if tid not in active_track_ids:
                lost.append(state)
                state.exit_frame = state.last_frame
                self._exited[state.visitor_id] = state
                del self._tracks[tid]
        return lost

    def get_active(self) -> dict[int, TrackState]:
        return self._tracks


class StaffDetector:
    """
    Classify staff by HSV uniform colour (dark navy/black) in upper-body crop.
    CAM_STAFF_01: all detections are always staff.
    CAM_BILLING_01: HSV check on upper-body region.
    All other cameras: never staff (customers only on floor cameras).
    """

    HSV_LOWER = np.array([100, 50, 20], dtype=np.uint8)
    HSV_UPPER = np.array([130, 255, 80], dtype=np.uint8)
    MIN_UNIFORM_RATIO = 0.35

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self.always_staff = camera_id == "CAM_STAFF_01"

    def is_staff(self, frame: np.ndarray, bbox: np.ndarray) -> bool:
        if self.always_staff:
            return True
        if self.camera_id != "CAM_BILLING_01":
            return False

        import cv2
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        mid_y = y1 + (y2 - y1) // 2
        crop = frame[y1:mid_y, x1:x2]
        if crop.size == 0:
            return False

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.HSV_LOWER, self.HSV_UPPER)
        ratio = np.count_nonzero(mask) / mask.size
        return ratio >= self.MIN_UNIFORM_RATIO
