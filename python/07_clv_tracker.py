#!/usr/bin/env python3
"""Closing line value (CLV) tracker: record a price now, capture the closing price.

CLV compares the odds you took with the last price before kickoff (the
"closing line"), which is the market's final and most informed estimate:

    CLV = taken_odds / closing_odds - 1

Took 2.10 and the line closed at 1.95 -> CLV = 2.10 / 1.95 - 1 = +7.7 %.
Beating the close consistently is the standard sign of a winning bettor.
The tracker also stores a no-vig version: closing_fair is the closing price
with the bookmaker margin removed (proportional method over the outcomes of
that line), and clv_fair = taken_odds / closing_fair - 1, which is a
stricter estimate of your edge.

Endpoint used: GET /kit/v1/prematch/lines?event_id=N&market_type=T
When a fixture kicks off it leaves the prematch board, so the last price
seen before that is the closing price. Keep `watch` running across kickoff.

Usage:
    export PINNRADAR_KEY=your_key
    # 1) record a bet (price defaults to the current price; --odds sets yours)
    python 07_clv_tracker.py take --event-id 1630412287 --market money_line --side home
    python 07_clv_tracker.py take --event-id 1630412287 --market totals --side over --line 2.75 --odds 2.08
    python 07_clv_tracker.py take --event-id 1630412287 --market spreads --side away --line 0.25
    # 2) leave this running; it samples each line in the last --lead seconds
    python 07_clv_tracker.py watch --lead 900 --interval 60
    # 3) results
    python 07_clv_tracker.py report

--line is the line of your side: for spreads, home -0.25 is --line -0.25 and
away +0.25 is --line 0.25; for totals it is the total (2.75).
Requests: one per fixture and market per --interval inside the lead window,
spaced by --min-interval; keep --interval at 60 s or more on a Free key
(100 requests per hour and per day).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

from pinnradar_client import (
    DEFAULT_MIN_INTERVAL,
    ApiError,
    Client,
    extract_event,
    get_key,
    parse_starts,
    run,
)

FIELDS = [
    "id", "event_id", "league", "home", "away", "starts", "period", "market", "side", "line",
    "taken_odds", "taken_at", "last_odds", "last_fair", "last_seen_at",
    "closing_odds", "closing_fair", "clv", "clv_fair", "status",
]
SIDES = {"money_line": ("home", "away", "draw"), "spreads": ("home", "away"),
         "totals": ("over", "under")}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def save(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)  # atomic: a crash never leaves half a file


def fair_odds(prices: dict[str, float], side: str) -> float | None:
    """No-vig price of `side`: normalise implied probabilities to sum to 1."""
    implied = {k: 1.0 / v for k, v in prices.items() if isinstance(v, (int, float)) and v > 1}
    if side not in implied or len(implied) < 2:
        return None
    return sum(implied.values()) / implied[side]


def find_price(event: dict, period: int, market: str, side: str,
               line: float | None) -> tuple[float | None, float | None]:
    """Return (price, fair price) of one outcome, or (None, None) if not offered."""
    p = (event.get("periods") or {}).get(f"num_{period}") or {}
    if market == "money_line":
        ml = p.get("money_line") or {}
        prices = {k: ml.get(k) for k in ("home", "away", "draw") if ml.get(k)}
        return prices.get(side), fair_odds(prices, side)
    if market == "spreads":
        # the row's hdp is the home handicap; away +x is the row with hdp -x
        want = line if side == "home" else -line
        for row in (p.get("spreads") or {}).values():
            if abs(float(row.get("hdp", 1e9)) - want) < 1e-9:
                prices = {"home": row.get("home"), "away": row.get("away")}
                return prices.get(side), fair_odds(prices, side)
        return None, None
    if market == "totals":
        for row in (p.get("totals") or {}).values():
            if abs(float(row.get("points", 1e9)) - line) < 1e-9:
                prices = {"over": row.get("over"), "under": row.get("under")}
                return prices.get(side), fair_odds(prices, side)
    return None, None


def fetch_event(client: Client, event_id: int, market: str) -> dict | None:
    """Current prematch lines of one event; None once it has left the board."""
    try:
        body = client.get("/kit/v1/prematch/lines",
                          {"event_id": event_id, "market_type": market})
    except ApiError as exc:
        if exc.status in (400, 404):
            return None
        raise
    return extract_event(body, event_id)


def fmt_pct(x: str | float | None) -> str:
    try:
        return f"{float(x):+.2%}"
    except (TypeError, ValueError):
        return "-"


# ------------------------------------------------------------------ commands

def cmd_take(args: argparse.Namespace, client: Client) -> None:
    if args.side not in SIDES[args.market]:
        sys.exit(f"error: --side for {args.market} must be one of {SIDES[args.market]}")
    if args.market != "money_line" and args.line is None:
        sys.exit("error: --line is required for spreads and totals")

    ev = fetch_event(client, args.event_id, args.market)
    if not ev:
        sys.exit(f"error: event {args.event_id} is not on the prematch board")
    price, fair = find_price(ev, args.period, args.market, args.side, args.line)
    if price is None:
        sys.exit("error: that line is not offered right now; list lines with "
                 f"04_prematch_lines.py --event-id {args.event_id} --market-type {args.market}")
    taken = args.odds or price

    rows = load(args.csv)
    row = {f: "" for f in FIELDS}
    row.update({
        "id": str(1 + max((int(r["id"]) for r in rows), default=0)),
        "event_id": str(args.event_id),
        "league": ev.get("league_name", ""), "home": ev.get("home", ""), "away": ev.get("away", ""),
        "starts": ev.get("starts", ""), "period": str(args.period),
        "market": args.market, "side": args.side,
        "line": "" if args.line is None else f"{args.line:g}",
        "taken_odds": f"{taken:.3f}", "taken_at": now_iso(),
        "last_odds": f"{price:.3f}", "last_fair": f"{fair:.3f}" if fair else "",
        "last_seen_at": now_iso(), "status": "open",
    })
    rows.append(row)
    save(args.csv, rows)
    print(f"#{row['id']} {row['home']} v {row['away']} {args.market} {args.side} "
          f"{row['line']} @ {taken:.3f} (market now {price:.3f}, kickoff {row['starts']})")


def close_row(row: dict) -> None:
    row["closing_odds"] = row["last_odds"]
    row["closing_fair"] = row["last_fair"]
    taken = float(row["taken_odds"])
    if row["closing_odds"]:
        row["clv"] = f"{taken / float(row['closing_odds']) - 1:.4f}"
    if row["closing_fair"]:
        row["clv_fair"] = f"{taken / float(row['closing_fair']) - 1:.4f}"
    row["status"] = "closed"
    print(f"closed #{row['id']} {row['home']} v {row['away']} {row['market']} {row['side']} "
          f"{row['line']}: took {row['taken_odds']}, closed {row['closing_odds']} "
          f"-> CLV {fmt_pct(row['clv'])} (no-vig {fmt_pct(row['clv_fair'])})")


def cmd_watch(args: argparse.Namespace, client: Client) -> None:
    print(f"watching {args.csv}: sampling every {args.interval:.0f}s in the last "
          f"{args.lead:.0f}s before kickoff (Ctrl-C to stop)")
    while True:
        rows = load(args.csv)
        open_rows = [r for r in rows if r["status"] == "open"]
        if not open_rows:
            print("no open bets; done.")
            return
        now = datetime.now(timezone.utc)

        # one request per (event, market) inside the lead window
        due: dict[tuple[int, str], list[dict]] = {}
        next_window = None
        for r in open_rows:
            starts = parse_starts(r["starts"])
            secs = (starts - now).total_seconds() if starts else 0
            if secs <= args.lead:
                due.setdefault((int(r["event_id"]), r["market"]), []).append(r)
            else:
                next_window = min(next_window or secs, secs - args.lead)

        for (event_id, market), group in due.items():
            ev = fetch_event(client, event_id, market)
            starts = parse_starts(group[0]["starts"])
            started = starts is not None and datetime.now(timezone.utc) >= starts
            if ev is None or started:
                # gone from the prematch board: the last price we saw is the close
                for r in group:
                    close_row(r)
                continue
            for r in group:
                line = float(r["line"]) if r["line"] else None
                price, fair = find_price(ev, int(r["period"]), market, r["side"], line)
                if price is not None:
                    r["last_odds"] = f"{price:.3f}"
                    r["last_fair"] = f"{fair:.3f}" if fair else ""
                    r["last_seen_at"] = now_iso()
                    print(f"{time.strftime('%H:%M:%S')} #{r['id']} {r['home']} v {r['away']} "
                          f"{market} {r['side']} {r['line']}: {price:.3f}")
                # if the exact line was taken down, keep the last price seen
            # starts can move (reschedules); keep the latest kickoff time
            if ev.get("starts"):
                for r in group:
                    r["starts"] = ev["starts"]
        save(args.csv, rows)

        if due:
            pause = args.interval
        else:
            pause = max(args.interval, min(next_window or args.interval, 3600))
            print(f"next lead window opens in {pause / 60:.0f} min; sleeping")
        time.sleep(pause)


def cmd_report(args: argparse.Namespace, _client: Client | None) -> None:
    rows = load(args.csv)
    if not rows:
        print(f"{args.csv} is empty; record a bet with `take` first.")
        return
    print(f"{'#':>3} {'match':<34} {'bet':<22} {'taken':>6} {'close':>6} {'CLV':>8} {'no-vig':>8}")
    clvs, fair = [], []
    for r in rows:
        bet = f"{r['market']} {r['side']} {r['line']}".strip()
        name = f"{r['home']} v {r['away']}"
        print(f"{r['id']:>3} {name[:34]:<34} {bet[:22]:<22} {r['taken_odds']:>6} "
              f"{r['closing_odds'] or 'open':>6} {fmt_pct(r['clv']):>8} {fmt_pct(r['clv_fair']):>8}")
        if r["clv"]:
            clvs.append(float(r["clv"]))
        if r["clv_fair"]:
            fair.append(float(r["clv_fair"]))
    if clvs:
        beat = sum(1 for x in clvs if x > 0)
        print(f"\n{len(clvs)} closed bets: average CLV {sum(clvs) / len(clvs):+.2%}, "
              f"beat the close {beat}/{len(clvs)}"
              + (f", average no-vig CLV {sum(fair) / len(fair):+.2%}" if fair else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", default="clv.csv", help="CSV file (default %(default)s)")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL,
                        help="smallest gap between requests in seconds (default %(default)s)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    take = sub.add_parser("take", help="record a bet at the current (or your) price")
    take.add_argument("--event-id", type=int, required=True)
    take.add_argument("--market", choices=tuple(SIDES), required=True)
    take.add_argument("--side", required=True, help="home/away/draw or over/under")
    take.add_argument("--line", type=float, help="your line (spreads, totals)")
    take.add_argument("--period", type=int, default=0, help="0 = whole match (default)")
    take.add_argument("--odds", type=float, help="the decimal odds you actually got")

    watch = sub.add_parser("watch", help="sample open bets up to kickoff and store the close")
    watch.add_argument("--lead", type=float, default=900,
                       help="start sampling this many seconds before kickoff (default 900)")
    watch.add_argument("--interval", type=float, default=60,
                       help="seconds between samples of each line (default 60)")

    sub.add_parser("report", help="print the CLV of every bet")
    args = parser.parse_args()

    if args.cmd == "report":
        cmd_report(args, None)  # works offline from the CSV
        return
    client = Client(get_key(), min_interval=args.min_interval)
    {"take": cmd_take, "watch": cmd_watch}[args.cmd](args, client)


if __name__ == "__main__":
    run(main)
