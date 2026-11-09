#!/usr/bin/env python3
"""Print every market of one event: all periods, full spread and total ladders.

Endpoints:
    live event:      GET /kit/v1/details?event_id=N
    prematch event:  GET /kit/v1/prematch/markets?event_id=N   (--prematch)

Take the event id from 02_live_odds_for_sport.py or 04_prematch_lines.py.

Usage:
    export PINNRADAR_KEY=your_key
    python 03_event_odds.py --event-id 1631874402
    python 03_event_odds.py --event-id 1630412287 --prematch
    python 03_event_odds.py --event-id 1630412287 --prematch --json > event.json

Periods: num_0 is the whole match, num_1 the first half, num_2 the second
half, and so on per sport. `max` is the largest stake accepted on that line.
"""

from __future__ import annotations

import argparse
import json

from pinnradar_client import (
    Client,
    extract_event,
    fmt_line,
    fmt_price,
    get_key,
    parse_starts,
    run,
)


def print_period(key: str, period: dict) -> None:
    desc = period.get("description") or ""
    meta = period.get("meta") or {}
    print(f"\n[{key}] {desc}  (max_total {meta.get('max_total', '-')})")

    ml = period.get("money_line") or {}
    if ml:
        row = f"  moneyline   home {fmt_price(ml.get('home'))}"
        if ml.get("draw"):
            row += f"   draw {fmt_price(ml.get('draw'))}"
        row += f"   away {fmt_price(ml.get('away'))}"
        print(row)

    spreads = period.get("spreads") or {}
    if spreads:
        print("  spreads     hdp      home    away    max")
        for row in sorted(spreads.values(), key=lambda r: r.get("hdp", 0)):
            print(f"              {fmt_line(row.get('hdp')):>6}  {fmt_price(row.get('home'))}   "
                  f"{fmt_price(row.get('away'))}   {row.get('max', '-')}")

    totals = period.get("totals") or {}
    if totals:
        print("  totals      points   over    under   max")
        for row in sorted(totals.values(), key=lambda r: r.get("points", 0)):
            print(f"              {row.get('points'):>6}  {fmt_price(row.get('over'))}   "
                  f"{fmt_price(row.get('under'))}   {row.get('max', '-')}")

    # team_totals has every alternate line; team_total only the main one per side
    team_totals = period.get("team_totals") or {}
    if any(team_totals.values()):
        for side in ("home", "away"):
            for row in sorted((team_totals.get(side) or {}).values(),
                              key=lambda r: r.get("points", 0)):
                print(f"  team total  {side:<4} {row.get('points'):>5}  over {fmt_price(row.get('over'))}"
                      f"   under {fmt_price(row.get('under'))}   max {row.get('max', '-')}")
    else:
        for side, row in (period.get("team_total") or {}).items():
            if row:
                print(f"  team total  {side:<4} {row.get('points'):>5}  over {fmt_price(row.get('over'))}"
                      f"   under {fmt_price(row.get('under'))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event-id", type=int, required=True, help="event (matchup) id")
    parser.add_argument("--prematch", action="store_true",
                        help="event has not started: use /kit/v1/prematch/markets")
    parser.add_argument("--json", action="store_true", help="print the raw JSON reply")
    args = parser.parse_args()

    client = Client(get_key())
    path = "/kit/v1/prematch/markets" if args.prematch else "/kit/v1/details"
    body = client.get(path, {"event_id": args.event_id})

    if args.json:
        print(json.dumps(body, indent=2))
        return

    ev = extract_event(body, args.event_id)
    if not ev:
        hint = ("it may have kicked off (try without --prematch)" if args.prematch
                else "it may not be in play (try --prematch) or has finished")
        print(f"event {args.event_id} not found: {hint}")
        raise SystemExit(1)

    starts = parse_starts(ev.get("starts"))
    print(f"{ev.get('home')} v {ev.get('away')}")
    print(f"{ev.get('league_name', '')} | {ev.get('event_type', '')} | "
          f"starts {starts:%Y-%m-%d %H:%M} UTC" if starts else ev.get("league_name", ""))
    if ev.get("updated_at"):
        print(f"markets last changed at {ev['updated_at']} (epoch ms)")

    periods = ev.get("periods") or {}
    if not periods:
        print("(no priced periods)")
    for key in sorted(periods, key=lambda k: int(k.split("_")[-1]) if k[-1].isdigit() else 99):
        print_period(key, periods[key])


if __name__ == "__main__":
    run(main)
