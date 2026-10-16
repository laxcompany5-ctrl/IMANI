"""A tiny stdlib HTTP client for IMANI's ingestion API.

Uses ``urllib`` only, so the agent needs no third-party HTTP library. Retries
transient failures (network errors and 5xx) but gives up immediately on a 4xx,
which means the payload itself is wrong.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class DeliveryError(RuntimeError):
    """Raised when a capture could not be delivered."""

    def __init__(self, message: str, status: int | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ImaniClient:
    """Posts captures to ``POST {endpoint}`` with the shared API key."""

    def __init__(self, endpoint: str, api_key: str, timeout: float = 10.0,
                 retries: int = 2, backoff: float = 1.5) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def send(self, payload: dict) -> dict:
        """Deliver one capture. Returns the parsed JSON response."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
                "User-Agent": "imani-capture-agent/1.0",
            },
        )

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")
                # A 4xx is the payload's fault — retrying will not help.
                if 400 <= error.code < 500:
                    raise DeliveryError(
                        f"IMANI rejected the capture ({error.code})",
                        status=error.code, body=detail,
                    ) from error
                last_error = DeliveryError(
                    f"IMANI error {error.code}", status=error.code, body=detail,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error

            if attempt < self.retries:
                time.sleep(self.backoff ** attempt)

        raise DeliveryError(f"could not reach IMANI: {last_error}")
