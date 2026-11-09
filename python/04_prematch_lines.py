#!/usr/bin/env python3
"""Upcoming fixtures of a sport, or the line ladder of one prematch event.

Endpoints:
    GET /kit/v1/prematch/fixtures?sport_id=N              (fixture list)
    GET /kit/v1/prematch/lines?event_id=N&market_type=T   (one event)

Usage:
    export PINNRADAR_KEY=your_key
    # soccer fixtures starting in the next 6 hours, filtered by words
    python 04_prematch_lines.py --sport 1 --hours 6 --query "premier league"
    # every totals line of one fixture
    python 04_prematch_lines.py --event-id 1630412287 --market-type totals

market_type is one of: money_line, spreads, totals, team_total (omit for all).
Each call is one request against your plan's limit.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from pinnradar_client import (
    Client,
    balanced_line,
    extract_event,
    fmt_line,
    fmt_price,
    get_key,
    parse_starts,
    run,
)

MARKET_TYPES = ("money_line", "spreads", "totals", "team_total")


def list_fixtures(client: Client, sport_id: int, hours: float, query: str, limit: int) -> None:
    body = client.get("/kit/v1/prematch/fixtures", {"sport_id": sport_id})
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours) if hours > 0 else None
    terms = query.lower().split()

    rows = []
    for ev in body.get("events") or []:
        # keep fixtures that already have a full-match price (as in the docs)
        if not ev.get("is_have_odds") or "num_0" not in (ev.get("periods") or {}):
            continue
        starts = parse_starts(ev.get("starts"))
        if horizon and starts and starts > horizon:
            continue
        text = f"{ev.get('league_name', '')} {ev.get('home', '')} {ev.get('away', '')}".lower()
        if terms and not all(t in text for t in terms):
            continue
        rows.append((starts or now, ev))
    rows.sort(key=lambda r: r[0])

    print(f"{body.get('sport_name', sport_id)}: {len(rows)} fixtures shown "
          f"(cursor {body.get('last')})\n")
    print(f"{'event_id':>11}  {'kickoff (UTC)':<16}  {'match':<46}  moneyline        total")
    for starts, ev in rows[:limit]:
        game = ev["periods"]["num_0"]
        ml = game.get("money_line") or {}
        ml_txt = "/".join(fmt_price(ml.get(s)) for s in ("home", "draw", "away") if ml.get(s))
        tot = balanced_line(game.get("totals") or {}, "over", "under")
        tot_txt = (f"{tot.get('points')} {fmt_price(tot.get('over'))}/{fmt_price(tot.get('under'))}"
                   if tot else "-")
        name = f"{ev.get('home')} v {ev.get('away')}"
        print(f"{ev.get('event_id'):>11}  {starts:%m-%d %H:%M}       {name[:46]:<46}  "
              f"{ml_txt:<16} {tot_txt}")
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more (raise --limit or narrow with --query)")


def show_lines(client: Client, event_id: int, market_type: str | None) -> None:
    params: dict = {"event_id": event_id}
    if market_type:
        params["market_type"] = market_type
    body = client.get("/kit/v1/prematch/lines", params)
    ev = extract_event(body, event_id)
    if not ev:
        print(f"event {event_id} is not on the prematch board (kicked off or withdrawn?)")
        raise SystemExit(1)

    if ev.get("home"):
        print(f"{ev.get('home')} v {ev.get('away')}  ({ev.get('league_name', '')})")
    periods = ev.get("periods") or {}
    for key in sorted(periods):
        period = periods[key]
        print(f"\n[{key}] {period.get('description', '')}")
        ml = period.get("money_line") or {}
        if ml:
            print("  money_line  " + "   ".join(f"{s} {fmt_price(ml[s])}"
                                              for s in ("home", "draw", "away") if ml.get(s)))
        for row in sorted((period.get("spreads") or {}).values(), key=lambda r: r.get("hdp", 0)):
            print(f"  spread {fmt_line(row.get('hdp')):>6}   home {fmt_price(row.get('home'))}"
                  f"   away {fmt_price(row.get('away'))}   max {row.get('max', '-')}")
        for row in sorted((period.get("totals") or {}).values(), key=lambda r: r.get("points", 0)):
            print(f"  total  {row.get('points'):>6}   over {fmt_price(row.get('over'))}"
                  f"   under {fmt_price(row.get('under'))}   max {row.get('max', '-')}")
        for side, row in (period.get("team_total") or {}).items():
            if row:
                print(f"  team total {side:<4} {row.get('points')}   over {fmt_price(row.get('over'))}"
                      f"   under {fmt_price(row.get('under'))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sport", type=int, help="list fixtures of this sport id")
    target.add_argument("--event-id", type=int, help="show the lines of one prematch event")
    parser.add_argument("--market-type", choices=MARKET_TYPES,
                        help="with --event-id: only this market")
    parser.add_argument("--hours", type=float, default=0,
                        help="with --sport: only fixtures starting within N hours (0 = all)")
    parser.add_argument("--query", default="", help="with --sport: words to match")
    parser.add_argument("--limit", type=int, default=40, help="rows to print (default 40)")
    args = parser.parse_args()

    client = Client(get_key())
    if args.sport is not None:
        list_fixtures(client, args.sport, args.hours, args.query, args.limit)
    else:
        show_lines(client, args.event_id, args.market_type)


if __name__ == "__main__":
    run(main)
