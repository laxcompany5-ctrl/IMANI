"""OpenCV-backed detection for the capture agent.

OpenCV is imported lazily and every entry point degrades gracefully, so the
rest of the agent (and its tests) run without it installed. Install it with::

    pip install -r agent/requirements.txt

Two detectors are used:

* a Histogram-of-Oriented-Gradients people detector (built into OpenCV — no
  model files to download), for whole-body boxes;
* Haar cascades for faces, used only to *optionally* produce a weak face
  descriptor when ``--face`` is given.

Neither is a substitute for a real detection/embedding model. They exist so the
pipeline is runnable end to end; swap them for your own in :class:`FrameAnalyzer`.
"""
from __future__ import annotations

from .traits import Box, Detection


def available() -> bool:
    """True when OpenCV is importable."""
    try:
        import cv2  # noqa: F401
    except Exception:  # pragma: no cover - depends on the environment
        return False
    return True


def _require_cv2():
    try:
        import cv2
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "OpenCV is not installed. Run: pip install -r agent/requirements.txt"
        ) from error
    return cv2


def open_capture(source: str):
    """Open a video source: a device index ("0"), a file, or an RTSP/HTTP URL."""
    cv2 = _require_cv2()
    if source.isdigit():
        source = int(source)
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        raise RuntimeError(f"could not open video source: {source!r}")
    return capture


class FrameAnalyzer:
    """Detects people (and optionally faces) in a frame."""

    def __init__(self, face: bool = False) -> None:
        self._cv2 = _require_cv2()
        self._hog = self._cv2.HOGDescriptor()
        self._hog.setSVMDetector(self._cv2.HOGDescriptor_getDefaultPeopleDetector())
        self._face_cascade = None
        if face:
            cascade_path = self._cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._face_cascade = self._cv2.CascadeClassifier(cascade_path)

    def detect(self, frame) -> list[Detection]:
        """Return whole-body detections for one frame."""
        rectangles, _weights = self._hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(16, 16), scale=1.05,
        )
        detections: list[Detection] = []
        for x, y, width, height in rectangles:
            detections.append(Detection(box=Box(float(x), float(y),
                                                float(width), float(height))))
        return detections

    def face_embedding(self, frame, box: Box) -> list[float] | None:
        """A *weak* face descriptor (histogram), or None.

        This is a placeholder so the face trait can be exercised. It is NOT a
        real face embedding and will match unreliably — replace it with a
        proper model before relying on face in production.
        """
        if self._face_cascade is None:
            return None
        cv2 = self._cv2
        x, y = int(max(box.x, 0)), int(max(box.y, 0))
        w, h = int(box.width), int(box.height)
        region = frame[y:y + h, x:x + w]
        if region.size == 0:
            return None
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        faces = self._face_cascade.detectMultiScale(gray, 1.1, 5)
        if len(faces) == 0:
            return None
        fx, fy, fw, fh = faces[0]
        face = gray[fy:fy + fh, fx:fx + fw]
        if face.size == 0:
            return None
        face = cv2.resize(face, (16, 16))
        histogram = cv2.calcHist([face], [0], None, [64], [0, 256])
        vector = histogram.flatten().tolist()
        total = sum(value * value for value in vector) ** 0.5
        if total == 0:
            return None
        return [value / total for value in vector]
