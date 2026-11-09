#!/usr/bin/env python3
"""Forward dropping-odds alerts from the SSE stream to a Telegram chat.

Reads /odds-drop (or /odds-drop-prematch) and posts the drops that pass your
filters through the Telegram Bot API (sendMessage). Bursts are grouped into
one message every --batch-seconds so the bot stays inside Telegram's own
limits (about one message per second per chat, 20 per minute in groups).

Setup:
    1. Create a bot with @BotFather and copy its token.
    2. Send any message to the bot (or add it to a group), then read the chat
       id from https://api.telegram.org/bot<TOKEN>/getUpdates
    3. export PINNRADAR_KEY=your_key            # plan with SSE: Drops, Edge, Scale
       export TELEGRAM_BOT_TOKEN=123456:ABC...
       export TELEGRAM_CHAT_ID=123456789

Usage:
    python 08_telegram_drop_bot.py --min-drop 8
    python 08_telegram_drop_bot.py --sport 1 --sport 3 --min-edge 0.02
    python 08_telegram_drop_bot.py --prematch --recheck 30 --min-drop 6
    python 08_telegram_drop_bot.py --dry-run          # print instead of sending

Run only one copy per key: a second SSE connection on the same key closes the
first one.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time

import requests

from pinnradar_client import USER_AGENT, StreamStop, fmt_price, get_key, sse_frames

TELEGRAM_API = "https://api.telegram.org"
MAX_TEXT = 4000  # Telegram allows 4096 characters per message


def drop_pct(d: dict) -> float:
    f, t = d.get("from_price") or 0, d.get("to_price") or 0
    return (f - t) / f * 100.0 if f else 0.0


def edge(d: dict) -> float | None:
    nvp = d.get("nvp")
    return d["to_price"] / nvp - 1 if nvp and d.get("to_price") else None


def render(d: dict) -> str:
    market = d.get("sect", "?")
    if d.get("point") is not None:
        market += f" {d['point']}"
    if d.get("period"):
        market += f" (period {d['period']})"
    head = f"{d.get('sport', '')}: {d.get('home')} v {d.get('away')}"
    live = d.get("live") or {}
    if live.get("status"):
        score = live.get("score") or {}
        head += f" [{live['status']}" + (f" {score.get('home')}-{score.get('away')}" if score else "") + "]"
    body = (f"{d.get('league', '')}\n{market} | {d.get('outcome')}: "
            f"{fmt_price(d.get('from_price'))} -> {fmt_price(d.get('to_price'))} "
            f"(-{drop_pct(d):.1f}%)")
    e = edge(d)
    if e is not None:
        body += f" | nvp {fmt_price(d.get('nvp'))}, edge {e:+.1%}"
    return f"{head}\n{body}"


class TelegramSender(threading.Thread):
    """Background sender: batches queued drops and respects Telegram's 429s."""

    def __init__(self, token: str, chat_id: str, batch_seconds: float,
                 max_per_message: int, dry_run: bool) -> None:
        super().__init__(daemon=True)
        self.url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.batch_seconds = batch_seconds
        self.max_per_message = max_per_message
        self.dry_run = dry_run
        self.inbox: queue.Queue[str] = queue.Queue()
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    def send(self, text: str) -> None:
        if self.dry_run:
            print("---- telegram message ----\n" + text, flush=True)
            return
        for attempt in range(5):
            try:
                resp = self.http.post(self.url, json={"chat_id": self.chat_id, "text": text},
                                      timeout=15)
            except requests.RequestException as exc:
                print(f"[telegram] {exc.__class__.__name__}; retrying", file=sys.stderr)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return
            try:
                info = resp.json()
            except ValueError:
                info = {}
            if resp.status_code == 429:
                wait = (info.get("parameters") or {}).get("retry_after", 5)
                print(f"[telegram] rate limited; waiting {wait}s", file=sys.stderr)
                time.sleep(float(wait) + 0.5)
                continue
            # 400/401/403: wrong chat id, bot blocked, bad token; retrying will not help
            print(f"[telegram] HTTP {resp.status_code}: {info.get('description', resp.text[:200])}",
                  file=sys.stderr)
            return

    def run(self) -> None:
        while True:
            first = self.inbox.get()  # block until there is something to send
            time.sleep(self.batch_seconds)  # let a burst collect
            items = [first]
            while True:
                try:
                    items.append(self.inbox.get_nowait())
                except queue.Empty:
                    break
            shown, extra = items[: self.max_per_message], len(items) - self.max_per_message
            text = ""
            for block in shown:
                if len(text) + len(block) + 2 > MAX_TEXT:
                    extra += 1
                    continue
                text += ("\n\n" if text else "") + block
            if extra > 0:
                text += f"\n\n(+{extra} more drops in this burst)"
            try:
                self.send(text)
            finally:
                for _ in items:
                    self.inbox.task_done()

    def drain(self, timeout: float = 15.0) -> None:
        """Wait (up to `timeout` s) until queued drops have been sent."""
        deadline = time.monotonic() + timeout
        while self.inbox.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prematch", action="store_true", help="use /odds-drop-prematch")
    parser.add_argument("--min-drop", type=float, default=5.0,
                        help="minimum drop in percent (default 5; server minimum 1)")
    parser.add_argument("--recheck", type=int, default=0,
                        help="prematch only: deliver only drops that held N seconds")
    parser.add_argument("--sport", type=int, action="append", default=[],
                        help="only these sport ids (repeatable)")
    parser.add_argument("--min-edge", type=float,
                        help="only drops with to_price/nvp - 1 >= this (e.g. 0.02 = 2%%)")
    parser.add_argument("--batch-seconds", type=float, default=3.0,
                        help="group drops arriving within this many seconds (default 3)")
    parser.add_argument("--max-per-message", type=int, default=10,
                        help="drops listed per Telegram message (default 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print messages instead of sending them")
    args = parser.parse_args()

    key = get_key()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and (not token or not chat_id):
        sys.exit("error: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or use --dry-run)")

    sender = TelegramSender(token, chat_id, args.batch_seconds, args.max_per_message, args.dry_run)
    sender.start()

    path = "/odds-drop-prematch" if args.prematch else "/odds-drop"
    params: dict = {"min_drop": max(1.0, args.min_drop)}
    if args.prematch and args.recheck > 0:
        params["recheck"] = args.recheck
    sports = set(args.sport)
    print(f"forwarding {path} {params} to Telegram (Ctrl-C to stop)", file=sys.stderr)

    forwarded = 0
    try:
        for frame in sse_frames(path, key, params):
            if isinstance(frame, dict):
                if frame.get("type") == "connected":
                    print("stream connected", file=sys.stderr)
                continue
            for d in frame if isinstance(frame, list) else []:
                if sports and d.get("sport_id") not in sports:
                    continue
                if drop_pct(d) < args.min_drop:
                    continue
                if args.min_edge is not None:
                    e = edge(d)
                    if e is None or e < args.min_edge:
                        continue
                sender.inbox.put(render(d))
                forwarded += 1
    except StreamStop as exc:
        sender.drain()
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\nstopping; sending what is queued ({forwarded} drops forwarded).", file=sys.stderr)
        sender.drain()


if __name__ == "__main__":
    main()
