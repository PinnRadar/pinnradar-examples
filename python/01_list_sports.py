#!/usr/bin/env python3
"""List PinnRadar sport ids, optionally with how many events each board holds.

Sport ids are stable integers (docs: "Sport IDs"), so the table itself needs
no request. With --count the script calls GET /kit/v1/markets once per sport
and prints the number of in-play (or, with --prematch, upcoming) events.

Usage:
    export PINNRADAR_KEY=your_key
    python 01_list_sports.py                 # print the id table, no requests
    python 01_list_sports.py --count         # + live event counts (13 requests)
    python 01_list_sports.py --count --prematch

Cost: --count makes 13 requests (14 and 15 are skipped because they are
subsets of 10). A Free key allows 20 per minute and 100 per day, so the calls
are spaced by --min-interval seconds (default 3.1).
"""

from __future__ import annotations

import argparse

from pinnradar_client import DEFAULT_MIN_INTERVAL, SPORTS, Client, get_key, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", action="store_true",
                        help="call /kit/v1/markets per sport and show event counts")
    parser.add_argument("--prematch", action="store_true",
                        help="count upcoming fixtures instead of in-play events")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help="seconds between requests (default %(default)s; 0.1 is fine on paid REST plans)")
    args = parser.parse_args()

    key = get_key()

    if not args.count:
        print(f"{'id':>3}  sport")
        for sport_id, name in SPORTS.items():
            print(f"{sport_id:>3}  {name}")
        print("\nTip: add --count to see how many events each board holds right now.")
        return

    client = Client(key, min_interval=args.min_interval)
    board = "prematch" if args.prematch else "live"
    print(f"{'id':>3}  {'sport':<32} {board + ' events':>15}")
    total = 0
    for sport_id, name in SPORTS.items():
        if sport_id in (14, 15):
            print(f"{sport_id:>3}  {name:<32} {'(see 10)':>15}")
            continue
        body = client.get("/kit/v1/markets",
                          {"sport_id": sport_id, "event_type": board})
        n = len(body.get("events") or [])
        total += n
        print(f"{sport_id:>3}  {name:<32} {n:>15}", flush=True)
    print(f"\n{total} {board} events in total "
          f"(X-Data-Status of last reply: {client.last_headers.get('X-Data-Status', '?')})")


if __name__ == "__main__":
    run(main)
