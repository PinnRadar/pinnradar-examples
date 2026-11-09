# PinnRadar MCP server

PinnRadar runs a remote [Model Context Protocol](https://modelcontextprotocol.io) server,
so AI assistants can read live and prematch odds, price drops and feed status directly.

- Endpoint: `https://mcp.pinnradar.com/mcp`
- Transport: Streamable HTTP, stateless, POST only
- Auth: `Authorization: Bearer <your PinnRadar key>`, or OAuth 2.1 in clients that support it (claude.ai)
- Calls use your normal API key, count against the same plan limits and show in your dashboard.

Full page: https://pinnradar.com/mcp. No key yet? Get a free one at https://pinnradar.com/panel#signup.

Keep keys out of files you commit: use an environment variable or the client's secret prompt
where the client supports one (Claude Code, VS Code and Codex examples below do).

## Claude Code

```bash
claude mcp add -t http -H "Authorization: Bearer $PINNRADAR_KEY" pinnradar https://mcp.pinnradar.com/mcp
```

`/mcp` then lists `pinnradar` with 8 tools. For a team, put the entry in `.mcp.json` and
reference the variable instead of the key:

```json
{
  "mcpServers": {
    "pinnradar": {
      "type": "http",
      "url": "https://mcp.pinnradar.com/mcp",
      "headers": { "Authorization": "Bearer ${PINNRADAR_KEY}" }
    }
  }
}
```

## claude.ai

Settings, Connectors, Add custom connector:

1. Name `pinnradar`, URL `https://mcp.pinnradar.com/mcp`
2. Leave both OAuth client fields blank, press Add, then Connect
3. On the PinnRadar authorisation page, enter your key once and confirm

The connector then uses a short-lived token that renews itself. Removing the connector revokes access.

## Claude Desktop

Settings, Developer, Edit Config, add the block, restart the app:

```json
{
  "mcpServers": {
    "pinnradar": {
      "headers": { "Authorization": "Bearer PASTE_KEY_HERE" },
      "url": "https://mcp.pinnradar.com/mcp",
      "type": "http"
    }
  }
}
```

## Cursor

Global `~/.cursor/mcp.json` or per project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "pinnradar": {
      "headers": { "Authorization": "Bearer PASTE_KEY_HERE" },
      "url": "https://mcp.pinnradar.com/mcp"
    }
  }
}
```

## VS Code

`.vscode/mcp.json`. VS Code asks for the key once and stores it securely:

```json
{
  "inputs": [
    { "id": "radar-key", "type": "promptString", "description": "Your PinnRadar key", "password": true }
  ],
  "servers": {
    "pinnradar": {
      "type": "http",
      "headers": { "Authorization": "Bearer ${input:radar-key}" },
      "url": "https://mcp.pinnradar.com/mcp"
    }
  }
}
```

## Codex CLI

`~/.codex/config.toml`. The key itself stays in the `PINNRADAR_KEY` variable:

```toml
[mcp_servers.pinnradar]
bearer_token_env_var = "PINNRADAR_KEY"
url = "https://mcp.pinnradar.com/mcp"
```

## Claude API (MCP connector)

Send the header `anthropic-beta: mcp-client-2025-11-20` and add **both** arrays to the
Messages request body: `mcp_servers` declares the server, and the `mcp_toolset` entry in
`tools` exposes its tools to the model (one without the other is rejected).

```json
"mcp_servers": [
  { "name": "pinnradar", "type": "url",
    "url": "https://mcp.pinnradar.com/mcp",
    "authorization_token": "PASTE_KEY_HERE" }
],
"tools": [
  { "type": "mcp_toolset", "mcp_server_name": "pinnradar" }
]
```

Complete request with curl (keys from `ANTHROPIC_API_KEY` and `PINNRADAR_KEY`):

```bash
curl https://api.anthropic.com/v1/messages \
  --header "x-api-key: $ANTHROPIC_API_KEY" \
  --header "anthropic-version: 2023-06-01" \
  --header "anthropic-beta: mcp-client-2025-11-20" \
  --header "content-type: application/json" \
  --data @- <<EOF
{
  "model": "claude-opus-5-5",
  "max_tokens": 4096,
  "mcp_servers": [
    { "name": "pinnradar", "type": "url",
      "url": "https://mcp.pinnradar.com/mcp",
      "authorization_token": "$PINNRADAR_KEY" }
  ],
  "tools": [
    { "type": "mcp_toolset", "mcp_server_name": "pinnradar" }
  ],
  "messages": [
    { "role": "user", "content": "Which soccer prices dropped the most in the last 10 minutes?" }
  ]
}
EOF
```

## Tools

| Tool | What it does | Cost |
|---|---|---|
| `board_sports` | Every sport PinnRadar carries with its numeric id. | free |
| `find_matches` | In-play or upcoming fixtures of one sport, one short row per match with the full-game moneyline, narrowed by `query` words (team or league) and a row `limit`. Source of `event_id` values. | 1 call |
| `match_prices` | One match's moneyline, spreads, totals and (optionally) team totals for the periods you list; concise mode keeps the 5 lines around the main line, detailed returns the full ladder. | 1 call |
| `upcoming_lines` | The complete ladder of alternate lines for one market of one prematch match, for every period it is offered in. | 1 call |
| `price_moves` | Prices that just fell hard (steam moves), most recent first; filter by sport, market, period, drop size and age (default: last 10 minutes, 5% or more). | 1 call |
| `feed_health` | Whether odds are arriving: overall status, in-play matches held, age of the latest live update, prematch on or off. | 1 call |
| `my_usage` | The key's plan, request limits, expiry date and today's request count. | 1 call |
| `search_pinnradar_docs` | Full-text search over the PinnRadar developer docs (endpoints, limits, streams, sport ids, JSON shapes). | free |

Every paid tool call fetches live prices and counts as one request on your plan, the same as a
REST call (no cache in between). A Free key's 100 daily requests can run out in one long session;
after `rate_limited` the server stops calling upstream until the window resets. The SSE and
WebSocket streams are not offered over MCP; connect to them directly (see `../python` and `../node`).

Questions: Telegram [@PinnRadarSupport](https://t.me/PinnRadarSupport) or info@pinnradar.com.
