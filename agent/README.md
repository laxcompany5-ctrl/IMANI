# IMANI capture agent

A small, standalone program that runs **on the camera side**. It watches a
video source, tracks each person, turns the track into IMANI traits, and posts
one capture per person to the server's `/ingest/capture` API.

It is deliberately self-contained: it imports nothing from the IMANI server, so
it can be copied onto an edge device (a Raspberry Pi, a mini-PC by the recorder,
or a machine already running your detection stack) on its own.

```
 camera / DVR ──▶ capture agent ──▶ POST /ingest/capture ──▶ IMANI
                   (this folder)         (X-API-Key)
```

## What it does

1. Reads frames from a device, file, or RTSP/HTTP stream (OpenCV).
2. Detects people with OpenCV's built-in HOG detector and tracks them across
   frames (a small IoU tracker).
3. Estimates traits from each track:
   * **height** from the box size and the camera's height above the ground
     (a pinhole-model estimate — see below);
   * **build** from the box width/height ratio;
   * **posture** from the head-to-feet lean, when keypoints are available;
   * **gait** from the person's speed in body-heights per second;
   * **face** only if `--face` is given (a *weak* placeholder descriptor).
4. Posts each confirmed track once (with a cooldown), so a person loitering in
   frame is not reported fifty times a second.

IMANI then stores the sighting and re-identifies the person, attaching them to
their existing record or auto-enrolling a new one.

## Quick start (no camera needed)

```bash
# From the repository root. Prints the captures it *would* send.
python -m agent --source synthetic --dry-run --camera gate-3
```

## Sending to IMANI

```bash
pip install -r agent/requirements.txt          # OpenCV, only needed for real video

export IMANI_INGEST_API_KEY="<the shared key from the server's .env>"

python -m agent \
  --source "rtsp://user:pass@camera.local/stream" \
  --endpoint "https://imani.example/ingest/capture" \
  --camera "gate-3" \
  --camera-height 250 --principal-y 360
```

| Flag | Meaning |
|------|---------|
| `--source` | `0` (default webcam), a file, an RTSP/HTTP URL, or `synthetic` |
| `--endpoint` | full URL of `/ingest/capture` |
| `--api-key` | the shared key (or set `IMANI_INGEST_API_KEY`) |
| `--camera` | the camera's name, stored on every sighting |
| `--camera-height` | how high the lens sits above the ground, in cm |
| `--principal-y` | image row of the optical centre / horizon, in px |
| `--every-n-frames` | analyse every Nth frame (default 5) |
| `--confirm-hits` | frames a person must be seen before a capture is sent |
| `--min-interval` | minimum seconds between captures of the same track |
| `--face` | also compute the (weak) face descriptor |
| `--dry-run` | print captures instead of sending them |

## About the height estimate

For a roughly level camera, the pinhole model on the ground plane gives

```
real_height ≈ box_height_px × camera_height_cm / (feet_y − principal_y)
```

so `--camera-height` and `--principal-y` matter. Get them roughly right (or
measure `principal_y` once by pointing the camera at the horizon), and the
estimate is usable; get them wrong and the height will be systematically off —
IMANI re-normalises across the traits it has, but a good calibration is worth a
minute of your time.

## Honest limitations

The trait extractors here are **simple heuristics, not trained models**. They
are good enough to demonstrate and to bootstrap a deployment, but for
production accuracy you should replace them:

* swap the HOG detector for a modern person detector (e.g. YOLO);
* replace the histogram "face" descriptor with a real face-embedding model
  (ArcFace/InsightFace, `face_recognition`) in `vision.FrameAnalyzer.face_embedding`;
* add a gait model if gait must carry weight.

All three are drop-in: keep returning the same shapes (`Box`, `Detection`,
`list[float]`) and the rest of the agent — tracking, payloads, delivery — is
unchanged.

## Legal note

Capturing and re-identifying people is regulated in most jurisdictions
(biometric data protection, CCTV signage, retention limits). Make sure the
deployment has the lawful basis and notices it needs.
