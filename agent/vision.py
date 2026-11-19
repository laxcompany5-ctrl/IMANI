"""OpenCV-backed detection for the capture agent.

OpenCV is imported lazily and every entry point degrades gracefully, so the
rest of the agent (and its tests) run without it installed. Install it with::

    pip install -r agent/requirements.txt

Two detectors are used:

* a Histogram-of-Oriented-Gradients people detector where the installed OpenCV
  build provides it (no model files to download);
* a motion/foreground detector (MOG2 + contours) as a fallback. Some OpenCV 5
  builds ship without HOG or Haar cascades — without this fallback a real
  RTSP/file/HTTP camera would fail before any frame was recorded.

Haar cascades are used only to *optionally* produce a weak face descriptor when
``--face`` is given and the build provides them.

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
        cv2 = self._cv2

        # HOG is preferred when this OpenCV build exposes it. OpenCV 5.0.0
        # headless builds may not, so fall back to foreground detection rather
        # than refusing to open a real camera.
        self._hog = None
        if (hasattr(cv2, "HOGDescriptor")
                and hasattr(cv2, "HOGDescriptor_getDefaultPeopleDetector")):
            try:
                self._hog = cv2.HOGDescriptor()
                self._hog.setSVMDetector(
                    cv2.HOGDescriptor_getDefaultPeopleDetector()
                )
            except Exception:  # pragma: no cover - build-specific
                self._hog = None
        self._background = None
        if self._hog is None:
            self._background = cv2.createBackgroundSubtractorMOG2(
                history=500, varThreshold=24, detectShadows=True,
            )

        self._face_cascade = None
        if face and hasattr(cv2, "CascadeClassifier"):
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._face_cascade = cv2.CascadeClassifier(cascade_path)

    def detect(self, frame) -> list[Detection]:
        """Return whole-body detections for one frame."""
        if self._hog is not None:
            rectangles, _weights = self._hog.detectMultiScale(
                frame, winStride=(8, 8), padding=(16, 16), scale=1.05,
            )
            return [
                Detection(box=Box(float(x), float(y),
                                  float(width), float(height)))
                for x, y, width, height in rectangles
            ]
        return self._detect_motion(frame)

    def _detect_motion(self, frame) -> list[Detection]:
        """Fallback detector for OpenCV builds without HOG.

        Uses background subtraction and upright contours. It is a real pixel
        analysis — not a demo — but it is only a coarse person proxy: it can
        confuse luggage or shadow movement. Keep HOG (or a proper model) where
        available for better identity measurements.
        """
        cv2 = self._cv2
        foreground = self._background.apply(frame)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel)
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(
            foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        height, width = frame.shape[:2]
        min_area = max(400, int(width * height * 0.002))
        detections: list[Detection] = []
        for contour in contours:
            if cv2.contourArea(contour) < min_area:
                continue
            x, y, box_w, box_h = cv2.boundingRect(contour)
            if box_w < 20 or box_h < 40:
                continue
            if box_h / max(box_w, 1) < 1.1:  # person-like, not a wide object
                continue
            if box_w > width * 0.7 or box_h > height * 0.95:
                continue  # whole-frame change is not a person
            center_x = float(x + box_w / 2)
            detections.append(Detection(
                box=Box(float(x), float(y), float(box_w), float(box_h)),
                head=(center_x, float(y + box_h * 0.12)),
                feet=(center_x, float(y + box_h)),
            ))
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


def encode_jpeg(frame, quality: int = 80) -> bytes | None:
    """Encode a frame as JPEG bytes for the live view."""
    cv2 = _require_cv2()
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    return buffer.tobytes()


def _read_mjpeg(url_or_stream, stop_event, max_timeout=10.0):
    """Very simple MJPEG reader using only stdlib (fallback when no OpenCV)."""
    try:
        from urllib.request import urlopen, Request
        import io
        req = Request(url_or_stream, headers={'User-Agent': 'IMANI/1.0'})
        resp = urlopen(req, timeout=max_timeout)
        data = bytearray()
        boundary = None
        ct = resp.headers.get('Content-Type') or resp.headers.get('content-type') or ''
        if 'boundary=' in ct:
            boundary = ct.split('boundary=')[-1].strip().strip('"')
        while not stop_event.is_set():
            chunk = resp.read(4096)
            if not chunk:
                break
            data.extend(chunk)
            if boundary:
                sep = ('--' + boundary).encode()
                while sep in data:
                    idx = data.find(sep)
                    # skip boundary
                    start = idx + len(sep)
                    # find header end
                    he = data.find(b'\r\n\r\n', start)
                    if he < 0:
                        break
                    hdr = bytes(data[start:he])
                    # find jpeg
                    soj = data.find(b'\xff\xd8', he)
                    eoj = data.find(b'\xff\xd9', he)
                    if soj >= 0 and eoj >= 0 and eoj > soj:
                        jpg = bytes(data[soj:eoj+2])
                        yield jpg
                        data = data[eoj+2:]
                    else:
                        data = data[he+4:]
            else:
                # try to find any jpeg frame
                soj = data.find(b'\xff\xd8')
                eoj = data.find(b'\xff\xd9')
                if soj >= 0 and eoj >= 0 and eoj > soj and len(data) > 10_000:
                    jpg = bytes(data[soj:eoj+2])
                    yield jpg
                    data = data[eoj+2:]
        resp.close()
    except Exception as e:
        raise
