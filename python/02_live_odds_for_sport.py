#!/usr/bin/env python3
"""Print the live board of one sport: score, moneyline, main spread and total.

Endpoint: GET /kit/v1/markets?sport_id=N&event_type=live
With --watch the script keeps polling with the documented delta cursor
(`since=<last>`), so later calls return only events that changed.

Usage:
    export PINNRADAR_KEY=your_key
    python 02_live_odds_for_sport.py --sport 1
    python 02_live_odds_for_sport.py --sport 3 --query "nba"
    python 02_live_odds_for_sport.py --sport 2 --watch 30      # poll every 30 s

Rate limits: a Free or Drops key allows 20 requests per minute, 100 per hour
and 100 per day, so keep --watch at 60 s or more on those plans. REST, Edge
and Scale keys (10 or 30 requests per second) can poll every second.

The "main" spread and total shown here are the lines whose two prices are
closest to each other; the full ladders are in 03_event_odds.py.
"""

from __future__ import annotations

import argparse
import time

from pinnradar_client import (
    DEFAULT_MIN_INTERVAL,
    SPORTS,
    Client,
    balanced_line,
    fmt_line,
    fmt_price,
    get_key,
    run,
)

FULL_REFRESH_EVERY = 10  # every Nth poll is a full one, which drops finished events


def describe(ev: dict) -> str:
    game = (ev.get("periods") or {}).get("num_0") or {}
    meta = game.get("meta") or {}
    score = ""
    if ev.get("event_type") == "live" and "home_score" in meta:
        score = f" [{meta.get('home_score')}-{meta.get('away_score')}]"
    ml = game.get("money_line") or {}
    ml_txt = f"ML {fmt_price(ml.get('home'))}"
    if ml.get("draw"):
        ml_txt += f" / {fmt_price(ml.get('draw'))}"
    ml_txt += f" / {fmt_price(ml.get('away'))}"
    parts = [ml_txt]
    spread = balanced_line(game.get("spreads") or {}, "home", "away")
    if spread:
        parts.append(f"HDP {fmt_line(spread.get('hdp'))} "
                     f"{fmt_price(spread.get('home'))}/{fmt_price(spread.get('away'))}")
    total = balanced_line(game.get("totals") or {}, "over", "under")
    if total:
        parts.append(f"O/U {total.get('points')} "
                     f"{fmt_price(total.get('over'))}/{fmt_price(total.get('under'))}")
    name = f"{ev.get('home')} v {ev.get('away')}{score}"
    return f"{ev.get('event_id'):>11}  {name[:44]:<44}  {'   '.join(parts)}"


def matches(ev: dict, terms: list[str]) -> bool:
    text = f"{ev.get('league_name', '')} {ev.get('home', '')} {ev.get('away', '')}".lower()
    return all(t in text for t in terms)


def print_board(events: list[dict], terms: list[str]) -> None:
    by_league: dict[str, list[dict]] = {}
    for ev in events:
        if matches(ev, terms):
            by_league.setdefault(ev.get("league_name") or "?", []).append(ev)
    if not by_league:
        print("(no events match)")
    for league in sorted(by_league):
        print(f"\n{league}")
        for ev in by_league[league]:
            print("  " + describe(ev))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sport", type=int, required=True,
                        help="sport id (1 Soccer, 2 Tennis, 3 Basketball, ...; see 01_list_sports.py)")
    parser.add_argument("--prematch", action="store_true",
                        help="read the prematch board (event_type=prematch) instead of live")
    parser.add_argument("--query", default="",
                        help="keep events whose league/home/away contain every word")
    parser.add_argument("--watch", type=float, default=0,
                        help="poll every N seconds with the since= cursor (0 = run once)")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help="smallest gap between requests in seconds (default %(default)s)")
    args = parser.parse_args()

    key = get_key()
    client = Client(key, min_interval=args.min_interval)
    terms = args.query.lower().split()
    event_type = "prematch" if args.prematch else "live"
    params = {"sport_id": args.sport, "event_type": event_type}

    body = client.get("/kit/v1/markets", params)
    book = {ev["event_id"]: ev for ev in body.get("events") or []}
    cursor = body.get("last")
    sport = body.get("sport_name") or SPORTS.get(args.sport, args.sport)
    age = client.last_headers.get("X-Data-Age-Ms")
    print(f"{sport}: {len(book)} {event_type} events (cursor {cursor}"
          f"{f', data age {age} ms' if age else ''})")
    print_board(list(book.values()), terms)

    if args.watch <= 0:
        return

    poll = 0
    while True:
        time.sleep(args.watch)
        poll += 1
        if poll % FULL_REFRESH_EVERY == 0:
            body = client.get("/kit/v1/markets", params)
            gone = set(book) - {ev["event_id"] for ev in body.get("events") or []}
            book = {ev["event_id"]: ev for ev in body.get("events") or []}
            cursor = body.get("last")
            print(f"\n--- full refresh {time.strftime('%H:%M:%S')}: {len(book)} events, "
                  f"{len(gone)} gone since last full read")
            continue

        body = client.get("/kit/v1/markets", {**params, "since": cursor})
        changed = body.get("events") or []
        cursor = body.get("last", cursor)
        for ev in changed:
            book[ev["event_id"]] = ev
        shown = [ev for ev in changed if matches(ev, terms)]
        print(f"\n--- {time.strftime('%H:%M:%S')}: {len(changed)} changed (cursor {cursor})")
        for ev in shown:
            print("  " + describe(ev))


if __name__ == "__main__":
    run(main)
