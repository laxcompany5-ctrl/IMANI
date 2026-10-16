"""A tiny IoU tracker.

A camera sees the same person in dozens of consecutive frames. Posting a
capture for every one of them would flood IMANI with near-duplicates, so we
track boxes across frames and emit one capture per *track* (with a cooldown).

This is intentionally minimal — enough to be useful and to make the agent's
behaviour testable without a real camera.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .traits import Box, Detection


@dataclass
class Track:
    """One person followed across frames."""

    id: int
    detection: Detection
    first_seen: float
    last_seen: float
    hits: int = 1
    missed: int = 0
    emitted_at: float | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=30))

    @property
    def box(self) -> Box:
        return self.detection.box

    def update(self, detection: Detection, timestamp: float) -> None:
        self.detection = detection
        self.last_seen = timestamp
        self.hits += 1
        self.missed = 0
        self.history.append((timestamp, detection.box))

    def speed(self) -> float | None:
        """Horizontal speed in body-heights per second, from the track history."""
        if len(self.history) < 2:
            return None
        first_ts, first_box = self.history[0]
        last_ts, last_box = self.history[-1]
        dt = last_ts - first_ts
        if dt <= 0:
            return None
        scale = (first_box.height + last_box.height) / 2
        if scale <= 0:
            return None
        dx = last_box.center[0] - first_box.center[0]
        return abs(dx) / scale / dt


class IouTracker:
    """Greedy intersection-over-union tracker with a missed-frame budget."""

    def __init__(self, iou_threshold: float = 0.3, max_missed: int = 15) -> None:
        self.iou_threshold = iou_threshold
        self.max_missed = max_missed
        self._tracks: dict[int, Track] = {}
        self._next_id = 1

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def update(self, detections: list[Detection], timestamp: float) -> list[Track]:
        """Match detections to existing tracks, creating or retiring as needed."""
        # Score every (track, detection) pair and take the best matches first.
        pairs = []
        for track in self._tracks.values():
            for index, detection in enumerate(detections):
                score = track.box.iou(detection.box)
                if score >= self.iou_threshold:
                    pairs.append((score, track.id, index))
        pairs.sort(reverse=True)

        matched_tracks: set[int] = set()
        matched_detections: set[int] = set()
        for _score, track_id, index in pairs:
            if track_id in matched_tracks or index in matched_detections:
                continue
            self._tracks[track_id].update(detections[index], timestamp)
            matched_tracks.add(track_id)
            matched_detections.add(index)

        # Unmatched detections start new tracks.
        for index, detection in enumerate(detections):
            if index in matched_detections:
                continue
            track = Track(id=self._next_id, detection=detection,
                          first_seen=timestamp, last_seen=timestamp)
            track.history.append((timestamp, detection.box))
            self._tracks[self._next_id] = track
            self._next_id += 1

        # Age out tracks we have not seen for a while.
        for track_id in list(self._tracks):
            track = self._tracks[track_id]
            if track_id not in matched_tracks:
                track.missed += 1
                if track.missed > self.max_missed:
                    del self._tracks[track_id]

        return self.tracks

    def should_emit(self, track: Track, timestamp: float, confirm_hits: int,
                    min_interval: float) -> bool:
        """A track is worth posting once confirmed, then only every so often."""
        if track.hits < confirm_hits:
            return False
        if track.emitted_at is None:
            return True
        return (timestamp - track.emitted_at) >= min_interval

    def mark_emitted(self, track: Track, timestamp: float) -> None:
        track.emitted_at = timestamp
