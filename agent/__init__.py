"""IMANI capture agent.

A standalone, camera-side program that watches a video source, turns each
detected person into a set of traits, and posts them to IMANI's
``/ingest/capture`` API. It is intentionally kept free of any dependency on the
IMANI server package, so it can be copied onto an edge device on its own.

The OpenCV-backed pieces live in :mod:`agent.vision` and are imported lazily;
everything else (trait maths, tracking, the API client) runs with the standard
library alone and is unit-tested.
"""

__all__ = ["traits", "tracker", "client", "agent"]
