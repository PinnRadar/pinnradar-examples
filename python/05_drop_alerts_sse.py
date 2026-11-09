#!/usr/bin/env python3
"""Follow the dropping-odds SSE stream and print each drop as it happens.

Streams (Server-Sent Events, one long-lived HTTP connection):
    live:      GET /odds-drop?min_drop=N
    prematch:  GET /odds-drop-prematch?min_drop=N&recheck=S     (--prematch)

Needs a plan with SSE (Drops, Edge or Scale). A Free or REST key gets
`plan_lacks_sse` and the script stops.

Usage:
    export PINNRADAR_KEY=your_key
    python 05_drop_alerts_sse.py                          # live, drops >= 5 %
    python 05_drop_alerts_sse.py --min-drop 8 --sport 1 --sport 2
    python 05_drop_alerts_sse.py --prematch --recheck 30  # only drops that held 30 s
    python 05_drop_alerts_sse.py --jsonl drops.jsonl      # also append raw drops

Notes:
- One SSE connection per account and stream (live and prematch are separate
  slots). Starting a second copy with the same key closes the first.
- On disconnect the script reconnects with exponential backoff (1 s up to
  60 s) and honours the server's Retry-After on `rate_limited`.
- The stream has no replay: drops sent while you were disconnected are not
  resent. GET /api/drops?max_age_sec=... can fill such a gap.
- `edge` below is to_price / nvp - 1, with nvp the no-vig fair price.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime

from pinnradar_client import StreamStop, fmt_price, get_key, sse_frames


def drop_pct(d: dict) -> float | None:
    f, t = d.get("from_price"), d.get("to_price")
    if isinstance(f, (int, float)) and isinstance(t, (int, float)) and f > 0:
        return (f - t) / f * 100.0
    return None


def format_drop(d: dict) -> str:
    pct = drop_pct(d)
    nvp = d.get("nvp")
    edge = d["to_price"] / nvp - 1 if nvp and d.get("to_price") else None
    when = datetime.fromtimestamp(d["alerted"]).strftime("%H:%M:%S") if d.get("alerted") else "--:--:--"
    market = d.get("sect", "?")
    if d.get("point") is not None:
        market += f" {d['point']}"
    if d.get("period"):
        market += f" (period {d['period']})"
    state = ""
    live = d.get("live") or {}
    if live.get("status"):
        state = f"  [{live['status']}"
        score = live.get("score")
        if score:
            state += f" {score.get('home')}-{score.get('away')}"
        state += "]"
    line1 = f"{when}  {d.get('sport', '?'):<10} {d.get('home')} v {d.get('away')}{state}"
    line2 = (f"          {d.get('league', '')} | {market} | {d.get('outcome')}: "
             f"{fmt_price(d.get('from_price'))} -> {fmt_price(d.get('to_price'))}")
    if pct is not None:
        line2 += f"  (-{pct:.1f}%)"
    if edge is not None:
        line2 += f"  nvp {fmt_price(nvp)}  edge {edge:+.1%}"
    if d.get("rechecked_ms"):
        line2 += f"  held {d['rechecked_ms'] / 1000:.0f}s"
    return f"{line1}\n{line2}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prematch", action="store_true", help="use /odds-drop-prematch")
    parser.add_argument("--min-drop", type=float, default=5.0,
                        help="server-side threshold in percent (default 5, minimum 1)")
    parser.add_argument("--recheck", type=int, default=0,
                        help="prematch only: deliver a drop only if it still holds after N seconds")
    parser.add_argument("--sport", type=int, action="append", default=[],
                        help="keep only these sport ids (repeatable; filtered on your side)")
    parser.add_argument("--jsonl", help="append every received drop as one JSON line to this file")
    args = parser.parse_args()

    key = get_key()
    path = "/odds-drop-prematch" if args.prematch else "/odds-drop"
    params: dict = {"min_drop": max(1.0, args.min_drop)}
    if args.prematch and args.recheck > 0:
        params["recheck"] = args.recheck
    sports = set(args.sport)

    print(f"connecting to {path} {params} ... (Ctrl-C to stop)", file=sys.stderr)
    with contextlib.ExitStack() as stack:
        out = stack.enter_context(open(args.jsonl, "a", encoding="utf-8")) if args.jsonl else None
        try:
            for frame in sse_frames(path, key, params):
                if isinstance(frame, dict):
                    if frame.get("type") == "connected":
                        print(f"connected (id {frame.get('id')}); waiting for drops", file=sys.stderr)
                    continue
                if not isinstance(frame, list):
                    continue
                for d in frame:
                    if sports and d.get("sport_id") not in sports:
                        continue
                    print(format_drop(d), flush=True)
                    if out:
                        out.write(json.dumps(d) + "\n")
                        out.flush()
        except StreamStop as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nstopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
