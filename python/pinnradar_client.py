"""Small shared helper for the PinnRadar Python examples.

Everything here uses the public, documented API (https://pinnradar.com/docs):

- the key travels in the ``x-portal-apikey`` header (read from PINNRADAR_KEY)
- HTTP 429 carries a ``Retry-After`` header (seconds) and a JSON body with
  ``retry_after_ms``; we wait for it instead of hammering the API
- HTTP 401 means ``missing_key`` / ``invalid_key``; retrying will not help
- REST responses carry ``X-Data-Status`` (fresh / stale / unknown),
  ``X-Data-Age-Ms`` and ``X-Data-Board`` (live / prematch)

The examples import this file from the same folder, so run them from
``python/`` (or anywhere: Python puts the script's folder on sys.path).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import requests

BASE = os.environ.get("PINNRADAR_BASE", "https://pinnradar.com").rstrip("/")
USER_AGENT = "pinnradar-examples/1.0 (+https://github.com/PinnRadar/pinnradar-examples)"

# Stable sport ids (docs section "Sport IDs"). 14 and 15 are filtered views of 10.
SPORTS: dict[int, str] = {
    1: "Soccer",
    2: "Tennis",
    3: "Basketball",
    4: "Hockey",
    5: "Football",
    6: "Baseball",
    7: "Rugby (union and league)",
    8: "MMA",
    9: "Boxing",
    10: "Other (volleyball + handball)",
    11: "Esports",
    12: "Golf",
    13: "Cricket",
    14: "Volleyball (subset of 10)",
    15: "Handball (subset of 10)",
}

# Free and Drops keys: 20/min, 100/hour, 100/day. 3.1 s between calls keeps a
# burst under 20 per minute. Paid REST plans (10 or 30 req/s) can go lower.
DEFAULT_MIN_INTERVAL = 3.1


class ApiError(Exception):
    """A non-retryable API error (bad key, plan without access, bad request...)."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(message or f"HTTP {status} {code}")


def get_key() -> str:
    """Return PINNRADAR_KEY or exit with a clear message (exit code 2)."""
    key = os.environ.get("PINNRADAR_KEY", "").strip()
    if not key:
        sys.stderr.write(
            "error: set PINNRADAR_KEY to your API key first.\n"
            "  Get a free key at https://pinnradar.com/panel#signup\n"
            "  bash:       export PINNRADAR_KEY=your_key\n"
            "  PowerShell: $env:PINNRADAR_KEY = 'your_key'\n"
        )
        sys.exit(2)
    return key


def error_code(resp: requests.Response) -> str:
    """The ``error`` field of a JSON error body, or the HTTP reason."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except ValueError:
        pass
    return resp.reason or "error"


def retry_after_seconds(resp: requests.Response, default: float = 5.0) -> float:
    """Seconds to wait after a 429: Retry-After header, else body retry_after_ms."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return max(1.0, float(header))
        except ValueError:
            pass
    try:
        ms = resp.json().get("retry_after_ms")
        if ms:
            return max(1.0, float(ms) / 1000.0)
    except (ValueError, AttributeError):
        pass
    return default


def explain(status: int, code: str) -> str:
    """Human hint for the documented error codes."""
    hints = {
        "missing_key": "no API key reached the server; check PINNRADAR_KEY",
        "invalid_key": "unknown key (typo, deleted, or replaced by a regenerated key)",
        "Unauthorized": "stream credential refused; check PINNRADAR_KEY",
        "plan_lacks_sse": "your plan has no SSE streams (Drops, Edge and Scale do)",
        "plan_lacks_ws": "your account has no active WebSocket add-on",
        "prematch_disabled": "the prematch feed is switched off right now",
        "rate_limited": "rate limit reached for your plan",
    }
    return hints.get(code, f"HTTP {status}")


class Client:
    """Thin REST client with pacing and polite 429 handling.

    min_interval: smallest gap between two requests, in seconds.
    max_wait: longest Retry-After we sleep through; above that (for example
              the daily window of a Free key) we stop with a clear message.
    """

    def __init__(
        self,
        key: str,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_wait: float = 120.0,
        timeout: float = 30.0,
    ) -> None:
        self.session = requests.Session()
        self.session.headers["x-portal-apikey"] = key
        self.session.headers["User-Agent"] = USER_AGENT
        self.min_interval = min_interval
        self.max_wait = max_wait
        self.timeout = timeout
        self._last_call = 0.0
        self.last_headers: dict[str, str] = {}

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON endpoint. Retries 429 (Retry-After), 5xx and network errors."""
        attempt = 0
        while True:
            self._pace()
            try:
                resp = self.session.get(BASE + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                attempt += 1
                if attempt > 5:
                    raise
                wait = min(30, 2**attempt)
                print(f"[network] {exc.__class__.__name__}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue

            self.last_headers = dict(resp.headers)
            if resp.status_code == 200:
                status = resp.headers.get("X-Data-Status")
                if status == "stale":
                    print("[note] X-Data-Status: stale (board held back; no fresh upstream data)",
                          file=sys.stderr)
                return resp.json()

            code = error_code(resp)
            if resp.status_code == 429:
                wait = retry_after_seconds(resp)
                if wait > self.max_wait:
                    window = ""
                    with contextlib.suppress(ValueError, AttributeError):
                        window = resp.json().get("window", "")
                    raise ApiError(429, "rate_limited",
                                   f"rate limited (window={window or '?'}); retry in {wait:.0f}s")
                print(f"[429] rate limited; waiting {wait:.0f}s (Retry-After)", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > 5:
                    raise ApiError(resp.status_code, code)
                wait = min(30, 2**attempt)
                print(f"[{resp.status_code}] {code}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise ApiError(resp.status_code, code, f"HTTP {resp.status_code} {code}: "
                           f"{explain(resp.status_code, code)}")


def run(main_fn) -> None:
    """Run main_fn() and turn ApiError / Ctrl-C into short messages and exit codes."""
    try:
        main_fn()
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        sys.exit(130)


# ---------------------------------------------------------------- SSE streams

class StreamStop(Exception):
    """Raised when reconnecting would not help (bad key, plan without SSE)."""


def sse_frames(
    path: str,
    key: str,
    params: dict[str, Any] | None = None,
    max_backoff: float = 60.0,
) -> Iterator[Any]:
    """Yield decoded JSON frames from /odds-drop or /odds-drop-prematch forever.

    Documented behaviour this follows:
    - first frame is {"type": "connected", "id": ...}; later frames are JSON
      arrays of drops
    - {"type": "error", "error": "plan_lacks_sse"} means the plan has no SSE: stop
    - {"type": "error", "error": "rate_limited"} (Retry-After: 60) means we
      reconnected too fast: back off exponentially
    - one SSE connection per account and stream: a second stream on the same
      key closes the first, so never run two copies of a script with one key

    The stream has no documented replay (no Last-Event-ID), so drops that
    happen while disconnected are not resent. Use GET /api/drops with
    max_age_sec to backfill a gap if you need it.
    """
    backoff = 1.0
    headers = {
        "x-portal-apikey": key,
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
    }
    while True:
        wait = backoff
        try:
            with requests.get(BASE + path, params=params, headers=headers,
                              stream=True, timeout=(10, None)) as resp:
                if resp.status_code in (401, 403):
                    code = error_code(resp)
                    raise StreamStop(f"HTTP {resp.status_code} {code}: "
                                     f"{explain(resp.status_code, code)}")
                if resp.status_code == 429:
                    wait = max(backoff, retry_after_seconds(resp, 60.0))
                    raise ConnectionError(f"HTTP 429 rate_limited; Retry-After {wait:.0f}s")
                if 400 <= resp.status_code < 500:
                    raise StreamStop(f"HTTP {resp.status_code} {error_code(resp)}")
                resp.raise_for_status()

                data_lines: list[str] = []
                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    line = raw.rstrip("\r")
                    if line == "":
                        # blank line = end of one SSE event
                        if data_lines:
                            payload = "\n".join(data_lines)
                            data_lines = []
                            try:
                                frame = json.loads(payload)
                            except ValueError:
                                continue
                            if isinstance(frame, dict) and frame.get("type") == "error":
                                err = frame.get("error")
                                if err == "plan_lacks_sse":
                                    raise StreamStop(explain(403, "plan_lacks_sse"))
                                if err == "rate_limited":
                                    wait = max(backoff, retry_after_seconds(resp, 60.0))
                                    raise ConnectionError(f"server says rate_limited; "
                                                          f"waiting {wait:.0f}s")
                                raise ConnectionError(f"server error frame: {err}")
                            if isinstance(frame, dict) and frame.get("type") == "connected":
                                backoff = 1.0  # healthy connection: reset the backoff
                            yield frame
                    elif line.startswith(":"):
                        continue  # SSE comment
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                raise ConnectionError("stream ended")
        except StreamStop:
            raise
        except (requests.RequestException, ConnectionError) as exc:
            print(f"[sse] {exc}; reconnecting in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            backoff = min(max_backoff, max(backoff, wait) * 2)


# ----------------------------------------------------------------- formatting

def fmt_price(value: Any) -> str:
    return f"{float(value):.3f}" if isinstance(value, (int, float)) else "-"


def fmt_line(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value:+g}" if value else "0"


def parse_starts(value: Any) -> datetime | None:
    """Parse ``starts``: ISO 8601 on REST events, Unix seconds on SSE drops."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def balanced_line(ladder: dict[str, Any], a: str, b: str) -> dict[str, Any] | None:
    """Pick the line whose two prices are closest (a fair stand-in for the main line)."""
    best, best_gap = None, None
    for row in (ladder or {}).values():
        pa, pb = row.get(a), row.get(b)
        if not isinstance(pa, (int, float)) or not isinstance(pb, (int, float)):
            continue
        gap = abs(pa - pb)
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    return best


def extract_event(body: Any, event_id: int | None = None) -> dict[str, Any] | None:
    """Return one event from a REST body: an ``events`` array or a bare event."""
    if isinstance(body, dict) and isinstance(body.get("events"), list):
        events = body["events"]
        if event_id is not None:
            for ev in events:
                if ev.get("event_id") == event_id:
                    return ev
        return events[0] if events else None
    if isinstance(body, dict) and "periods" in body:
        return body
    return None
