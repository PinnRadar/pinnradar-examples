#!/usr/bin/env python3
"""Consume the raw WebSocket feed (wss /ws/feed) and keep a merged order book.

The socket relays upstream frames unchanged inside a thin envelope. It needs
the WebSocket add-on on a REST, Edge or Scale plan; without it the server
answers HTTP 403 plan_lacks_ws and the script stops.

Usage:
    export PINNRADAR_KEY=your_key
    python 06_websocket_feed.py --sport 3                       # live basketball
    python 06_websocket_feed.py --sport 1 --stream live --stream prematch
    python 06_websocket_feed.py --event-id 1632907731 --verbose
    python 06_websocket_feed.py --sport 2 --deflate             # thin/distant link

Protocol (docs section "WebSocket feed"), as implemented here:
- within 5 s of connecting send
  {"type":"subscribe","streams":[...],"sport_ids":[...],"event_ids":[...]}
- `snapshot` frames are the baseline (large ones are split: `seq`, `final`)
- `live` frames: `op` add/upd/del, `rec` = upstream record; merge markets by
  `key` (or type|period|side|points), drop markets whose status is not "open"
  and every market of a period whose status is closed or settled
- `prematch_markets` carries the full market list of `matchup_id`
- `ping` every 30 s: answer {"type":"pong"} or the socket closes after ~75 s
- one socket per account: a newer connection evicts the older one (close
  1001 "evicted by newer connection"); this script then stops instead of
  fighting the other client
- reconnect backoff 1 s, 2 s, 4 s ... 30 s; subscribe again after each
  reconnect, since no state is kept between connections

Limits: at most 32 sport_ids per subscribe and 200 event_ids per stream.
Sport ids 14 and 15 are not accepted on the socket (their events arrive
under 10).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from collections import Counter
from urllib.parse import quote

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from pinnradar_client import USER_AGENT, get_key

WS_URL = "wss://pinnradar.com/ws/feed?key="
FATAL_ERRORS = {"sport_id_cap", "event_id_cap", "invalid_json", "invalid_message",
                "unknown_message_type"}


class Stop(Exception):
    """Reconnecting will not help."""


class Closed(Exception):
    """The socket closed; carries the close code and reason."""

    def __init__(self, code: int | None, reason: str, healthy: bool) -> None:
        super().__init__(f"{code} {reason}")
        self.code, self.reason, self.healthy = code, reason, healthy


class BookKeeper:
    """Python port of the merge recipe in the docs."""

    def __init__(self) -> None:
        self.records: dict[int, dict] = {}

    @staticmethod
    def id_of(mk: dict) -> str:
        if mk.get("key"):
            return mk["key"]
        prices = mk.get("prices") or []
        pts = prices[0].get("points", "") if prices else ""
        return "|".join(str(x) for x in (mk.get("type"), mk.get("period") or 0,
                                          mk.get("side") or "", "" if pts is None else pts))

    def apply(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "snapshot":
            for rec in frame.get("events") or []:
                if rec.get("id") is not None:
                    self.records[rec["id"]] = rec
        elif kind == "live" and frame.get("op") == "del":
            self.records.pop(frame["rec"].get("id"), None)
        elif kind == "live":
            self.merge_live(frame["rec"])
        elif kind == "prematch_markets":  # full list: overwrite
            mid = frame.get("matchup_id")
            self.records[mid] = {**self.records.get(mid, {}), "markets": frame.get("data") or []}

    def merge_live(self, rec: dict) -> None:
        prev = self.records.get(rec.get("id"), {})
        nxt = {**prev, **rec}
        book = {self.id_of(mk): mk for mk in prev.get("markets") or []}
        for mk in rec.get("markets") or []:
            mid = self.id_of(mk)
            if mk.get("status") and mk["status"] != "open":
                book.pop(mid, None)
            else:
                book[mid] = mk
        shut = {p.get("period") for p in rec.get("periods") or [] if p.get("status") != "open"}
        nxt["markets"] = [mk for mk in book.values() if (mk.get("period") or 0) not in shut]
        self.records[rec.get("id")] = nxt


def build_subscribe(args: argparse.Namespace) -> dict:
    msg: dict = {"type": "subscribe", "streams": args.stream or ["live"]}
    if args.sport:
        msg["sport_ids"] = args.sport
    if args.event_id:
        msg["event_ids"] = args.event_id
    return msg


async def session(args: argparse.Namespace, key: str, book: BookKeeper, stats: Counter) -> None:
    """One connection: subscribe, read until the socket closes, then raise Closed."""
    url = WS_URL + quote(key, safe="")
    async with connect(
        url,
        compression="deflate" if args.deflate else None,
        max_size=8 << 20,           # docs: 8 MB client limit (2 MB minimum)
        open_timeout=15,
        user_agent_header=USER_AGENT,
    ) as sock:
        sub = build_subscribe(args)
        await sock.send(json.dumps(sub))
        print(f"connected; sent {sub}", file=sys.stderr)
        book.records.clear()        # no state survives a reconnect
        stats.clear()
        with contextlib.suppress(ConnectionClosed):
            await read_frames(sock, sub, book, stats, args)
        raise Closed(sock.close_code, sock.close_reason or "", stats["snapshot"] > 0)


async def read_frames(sock, sub: dict, book: BookKeeper, stats: Counter,
                      args: argparse.Namespace) -> None:
    """Handle frames until the socket closes."""
    last_status = time.monotonic()
    async for text in sock:
        frame = json.loads(text)
        kind = frame.get("type", "?")
        stats[kind] += 1

        if kind == "ping":
            await sock.send('{"type":"pong"}')
            stats["buffered_max_bytes"] = frame.get("buffered_max_bytes", 0)
        elif kind in ("subscribed", "unsubscribed"):
            print(f"{kind}: stream={frame.get('stream')} sport_id={frame.get('sport_id')}")
        elif kind == "snapshot":
            book.apply(frame)
            part = f" part {frame['seq']}" if "seq" in frame else ""
            done = " (final)" if frame.get("final") else ""
            print(f"snapshot {frame.get('stream')} sport {frame.get('sport_id')}{part}{done}: "
                  f"{len(frame.get('events') or [])} events")
        elif kind == "live":
            book.apply(frame)
            if args.verbose:
                rec = frame.get("rec") or {}
                channel = str(frame.get("topic", "")).rsplit("/", 1)[-1]
                mode = f" mode={frame['mode']}" if frame.get("mode") else ""
                print(f"live {frame.get('op'):<3} id={rec.get('id')} ch={channel} "
                      f"markets={len(rec.get('markets') or [])}{mode}")
        elif kind == "prematch_markets":
            book.apply(frame)
        elif kind == "prematch_membership":
            added, removed = frame.get("added") or [], frame.get("removed") or []
            for mid in removed:
                book.records.pop(mid, None)
            if added or removed:
                print(f"membership sport {frame.get('sport_id')}: +{len(added)} -{len(removed)}")
        elif kind == "error":
            code = frame.get("code")
            print(f"server error: {code} {frame.get('message', '')}", file=sys.stderr)
            if code in FATAL_ERRORS:
                raise Stop(f"fix the subscribe message ({code})")
            if code == "backpressure":
                # the sport was NOT subscribed; keep reading, then retry once
                await asyncio.sleep(1)
                await sock.send(json.dumps(sub))

        if time.monotonic() - last_status >= 10:
            last_status = time.monotonic()
            busy = ", ".join(f"{k}={v}" for k, v in stats.most_common()
                             if k != "buffered_max_bytes")
            print(f"[status] {len(book.records)} records in book | frames: {busy} | "
                  f"buffered_max_bytes={stats['buffered_max_bytes']}")


async def main_async(args: argparse.Namespace, key: str) -> None:
    book, stats = BookKeeper(), Counter()
    backoff = 1.0
    while True:
        wait = backoff
        try:
            await session(args, key, book, stats)
        except Closed as exc:
            print(f"socket closed: {exc.code} {exc.reason}", file=sys.stderr)
            if "evicted by newer connection" in exc.reason:
                raise Stop("another connection on this account took over the socket "
                           "(one WebSocket per account)") from None
            if exc.healthy:
                backoff = wait = 1.0
            if exc.reason.startswith("deregistered: "):
                wait = 0.5  # server-side fault: reconnect right away
                if "slow_consumer" in exc.reason:
                    print("hint: your reader fell behind; process frames faster", file=sys.stderr)
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = exc.response.body.decode("utf-8", "replace") if exc.response.body else ""
            if status in (401, 403):
                hint = "no active WebSocket add-on" if "plan_lacks_ws" in body else "check PINNRADAR_KEY"
                raise Stop(f"HTTP {status} {body.strip()} ({hint})") from None
            if status == 429:
                try:
                    wait = max(backoff, float(exc.response.headers.get("Retry-After", 60)))
                except ValueError:
                    wait = max(backoff, 60.0)
            print(f"handshake refused: HTTP {status} {body.strip()}", file=sys.stderr)
        except (OSError, asyncio.TimeoutError) as exc:
            print(f"connection error: {exc!r}", file=sys.stderr)

        print(f"reconnecting in {wait:.1f}s", file=sys.stderr)
        await asyncio.sleep(wait)
        backoff = min(30.0, max(backoff, wait) * 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sport", type=int, action="append", default=[],
                        help="sport id to subscribe (repeatable, up to 32; not 14/15)")
    parser.add_argument("--event-id", type=int, action="append", default=[],
                        help="event id to subscribe (repeatable, up to 200 per stream)")
    parser.add_argument("--stream", action="append", choices=("live", "prematch"),
                        help="stream(s) to subscribe (default: live)")
    parser.add_argument("--deflate", action="store_true",
                        help="enable permessage-deflate (smaller, can add latency in bursts)")
    parser.add_argument("--verbose", action="store_true", help="print every live frame")
    args = parser.parse_args()

    key = get_key()
    if not args.sport and not args.event_id:
        parser.error("give at least one --sport or --event-id")
    if {14, 15} & set(args.sport):
        parser.error("the socket routes volleyball/handball under sport 10; use --sport 10")
    if len(args.sport) > 32 or len(args.event_id) > 200:
        parser.error("at most 32 sport ids and 200 event ids per subscribe")

    try:
        asyncio.run(main_async(args, key))
    except Stop as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
