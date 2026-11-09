# PinnRadar API examples

Working code examples for the [PinnRadar](https://pinnradar.com) odds API.

PinnRadar is an independent odds data service that serves Pinnacle live and prematch prices.
You get REST snapshots of every sport's board, a searchable buffer of recent price drops,
real-time drop alerts over Server-Sent Events, a raw WebSocket feed, and a remote MCP server
for AI assistants. It is not affiliated with Pinnacle, and you don't need a Pinnacle account.

**Get a free API key:** https://pinnradar.com/panel#signup

## Quick start

```bash
git clone https://github.com/PinnRadar/pinnradar-examples.git
cd pinnradar-examples

export PINNRADAR_KEY=your_key            # PowerShell: $env:PINNRADAR_KEY = "your_key"

# Python 3.9+
cd python
python -m pip install -r requirements.txt
python 02_live_odds_for_sport.py --sport 1

# Node 20+
cd ../node
npm install
node list-sports.mjs
```

Every example reads the key from the `PINNRADAR_KEY` environment variable and sends it in the
`x-portal-apikey` header. Never commit your key.

Or with plain curl:

```bash
curl -sG https://pinnradar.com/kit/v1/markets \
  --header "x-portal-apikey: $PINNRADAR_KEY" \
  -d sport_id=3 -d event_type=live
```

## Examples

### Python (`python/`)

| Script | What it shows |
|---|---|
| [01_list_sports.py](python/01_list_sports.py) | Sport ids, plus optional live or prematch event counts per sport |
| [02_live_odds_for_sport.py](python/02_live_odds_for_sport.py) | Live board of one sport (score, moneyline, main spread and total), with `since=` delta polling |
| [03_event_odds.py](python/03_event_odds.py) | Every period and full line ladder of one live or prematch event |
| [04_prematch_lines.py](python/04_prematch_lines.py) | Upcoming fixtures of a sport, or the lines of one fixture by market type |
| [05_drop_alerts_sse.py](python/05_drop_alerts_sse.py) | Live or prematch dropping-odds stream (SSE) with reconnect, backoff and no-vig edge |
| [06_websocket_feed.py](python/06_websocket_feed.py) | Raw WebSocket feed: subscribe, pong, snapshots, merged order book, close-code handling |
| [07_clv_tracker.py](python/07_clv_tracker.py) | Closing line value: record a price, capture the closing price, CLV to CSV |
| [08_telegram_drop_bot.py](python/08_telegram_drop_bot.py) | Forward filtered drop alerts to a Telegram chat through the Bot API |

`pinnradar_client.py` is the small shared helper used by the scripts: request pacing, 429
handling with `Retry-After`, clear 401/403 messages, and an SSE reader. Every script has
`--help`.

### Node.js (`node/`)

| Script | What it shows |
|---|---|
| [list-sports.mjs](node/list-sports.mjs) | Sport ids and optional event counts, using built-in `fetch` |
| [drop-alerts-sse.mjs](node/drop-alerts-sse.mjs) | Dropping-odds SSE stream with a dependency-free stream parser and reconnect |
| [websocket-feed.mjs](node/websocket-feed.mjs) | Raw WebSocket feed with the `ws` package and the documented merge recipe |

### MCP (`mcp/`)

[mcp/README.md](mcp/README.md) has config for Claude Code, claude.ai, Claude Desktop, Cursor,
VS Code, Codex CLI and the Claude API MCP connector, and lists the 8 tools.

## What each plan can use

Limits are counted per key in fixed windows. Going over a window returns HTTP 429 with a
`Retry-After` header. The examples wait for it rather than retrying in a loop.

| Plan | REST rate limit | SSE drop streams | WebSocket feed |
|---|---|---|---|
| Free | 20/min, 100/hour, 100/day | no | no |
| Drops | 20/min, 100/hour, 100/day | yes | no |
| REST | 10 requests/second | no | paid add-on |
| Edge | 10 requests/second | yes | paid add-on |
| Scale | 30 requests/second | yes | paid add-on |

- A Free key stops at 100 requests per day. After that, calls get 429 with `window=day` until UTC midnight.
- Each account can have one SSE connection per stream. Live and prematch are separate slots,
  so you can run one of each. A second connection on the same key closes the first.
- Each account can have one WebSocket connection. A newer one evicts the older one.
- Scripts that poll space their requests by `--min-interval` (default 3.1 s, which keeps a Free key
  under 20 per minute). On REST, Edge or Scale you can lower it.

Prices and billing periods: https://pinnradar.com/pinnacle-api-pricing

## Error codes you may see

| Status | Code | Meaning |
|---|---|---|
| 401 | `missing_key` / `invalid_key` | No key on the request, or the key is unknown |
| 403 | `plan_lacks_sse` | Valid key, but the plan has no SSE (Drops, Edge and Scale do) |
| 403 | `plan_lacks_ws` | Valid key, but the account has no active WebSocket add-on |
| 429 | `rate_limited` | Wait for `Retry-After` |
| 503 | `prematch_disabled` | The prematch feed is switched off right now |

REST responses also carry `X-Data-Status` (`fresh`, `stale` or `unknown`), `X-Data-Age-Ms` and
`X-Data-Board`. A `stale` board comes back in the normal empty shape, not as an error.

## Links

- API reference: https://pinnradar.com/docs
- Pricing: https://pinnradar.com/pinnacle-api-pricing
- MCP server: https://pinnradar.com/mcp
- Support: Telegram [@PinnRadarSupport](https://t.me/PinnRadarSupport) or info@pinnradar.com

## License

[MIT](LICENSE). Copyright (c) 2026 PinnRadar.
