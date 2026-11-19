"""Real OpenCV source compatibility tests."""
from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")
numpy = pytest.importorskip("numpy")

from agent.vision import FrameAnalyzer  # noqa: E402


def test_motion_fallback_works_without_hog(monkeypatch):
    """OpenCV 5 builds without HOG must still analyse real frames."""
    monkeypatch.delattr(cv2, "HOGDescriptor", raising=False)
    monkeypatch.delattr(cv2, "HOGDescriptor_getDefaultPeopleDetector", raising=False)

    analyzer = FrameAnalyzer()
    rng = numpy.random.default_rng(7)
    detections_total = 0

    for index in range(12):
        frame = numpy.full((240, 320, 3), 150, dtype=numpy.uint8)
        noise = rng.integers(0, 12, (240, 320, 3), dtype=numpy.uint8)
        frame = numpy.clip(
            frame.astype(numpy.int16) + noise, 0, 255
        ).astype(numpy.uint8)
        x = 40 + index * 12
        cv2.rectangle(frame, (x, 40), (x + 35, 200), (30, 30, 30), -1)
        detections_total += len(analyzer.detect(frame))

    assert detections_total > 0
