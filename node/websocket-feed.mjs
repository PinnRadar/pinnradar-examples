#!/usr/bin/env node
// Consume the raw WebSocket feed (wss /ws/feed) and keep a merged book.
//
// Needs the WebSocket add-on on a REST, Edge or Scale plan; without it the
// handshake is refused with HTTP 403 plan_lacks_ws and the script exits.
//
// Usage (Node 20+; run `npm install` once for the `ws` package):
//   export PINNRADAR_KEY=your_key
//   node websocket-feed.mjs --sport 3
//   node websocket-feed.mjs --sport 1 --stream live --stream prematch
//   node websocket-feed.mjs --event-id 1632907731 --verbose
//
// Protocol, as documented:
// - within 5 s of connecting send
//   {"type":"subscribe","streams":[...],"sport_ids":[...],"event_ids":[...]}
// - "snapshot" frames are the baseline (big ones split with seq / final)
// - "live" frames carry op (add/upd/del), topic, rec (upstream record), ts
// - "ping" every 30 s: answer {"type":"pong"} or the socket closes (~75 s)
// - one socket per account: a newer connection evicts the older one with
//   close 1001 "evicted by newer connection"; this script then exits
// - reconnect with backoff 1, 2, 4 ... 30 s and subscribe again
// Limits: 32 sport_ids per subscribe, 200 event_ids per stream, no 14/15.

import { parseArgs } from "node:util";
import WebSocket from "ws";

const KEY = (process.env.PINNRADAR_KEY ?? "").trim();
const WS_URL = "wss://pinnradar.com/ws/feed?key=";
const FATAL = new Set(["sport_id_cap", "event_id_cap", "invalid_json", "invalid_message", "unknown_message_type"]);

const { values: args } = parseArgs({
  options: {
    sport: { type: "string", multiple: true, default: [] },
    "event-id": { type: "string", multiple: true, default: [] },
    stream: { type: "string", multiple: true, default: [] },
    deflate: { type: "boolean", default: false },
    verbose: { type: "boolean", default: false },
  },
});

if (!KEY) {
  console.error("error: set PINNRADAR_KEY to your API key first.");
  console.error("  Get a free key at https://pinnradar.com/panel#signup");
  process.exit(2);
}

const sportIds = args.sport.map(Number);
const eventIds = args["event-id"].map(Number);
const streams = args.stream.length ? args.stream : ["live"];
if (!sportIds.length && !eventIds.length) {
  console.error("error: give at least one --sport or --event-id");
  process.exit(2);
}
if (sportIds.some((id) => id === 14 || id === 15)) {
  console.error("error: the socket routes volleyball/handball under sport 10; use --sport 10");
  process.exit(2);
}
if (streams.some((s) => s !== "live" && s !== "prematch")) {
  console.error("error: --stream must be live or prematch");
  process.exit(2);
}

const subscribe = { type: "subscribe", streams };
if (sportIds.length) subscribe.sport_ids = sportIds;
if (eventIds.length) subscribe.event_ids = eventIds;

/** Merge recipe from the docs. */
class BookKeeper {
  constructor() { this.records = new Map(); }

  static idOf(mk) {
    if (mk.key) return mk.key;
    const pts = mk.prices && mk.prices.length ? mk.prices[0].points : "";
    return [mk.type, mk.period || 0, mk.side || "", pts ?? ""].join("|");
  }

  apply(frame) {
    if (frame.type === "snapshot") frame.events.forEach((rec) => this.records.set(rec.id, rec));
    else if (frame.type === "live" && frame.op === "del") this.records.delete(frame.rec.id);
    else if (frame.type === "live") this.mergeLive(frame.rec);
    else if (frame.type === "prematch_markets")   // full list: overwrite
      this.records.set(frame.matchup_id, { ...this.records.get(frame.matchup_id), markets: frame.data });
  }

  mergeLive(rec) {
    const prev = this.records.get(rec.id) || {};
    const next = Object.assign({}, prev, rec);
    const book = new Map();
    for (const mk of prev.markets || []) book.set(BookKeeper.idOf(mk), mk);
    for (const mk of rec.markets || []) {
      const id = BookKeeper.idOf(mk);
      if (mk.status && mk.status !== "open") book.delete(id);
      else book.set(id, mk);
    }
    const shut = (rec.periods || []).filter((p) => p.status !== "open").map((p) => p.period);
    next.markets = [...book.values()].filter((mk) => !shut.includes(mk.period || 0));
    this.records.set(rec.id, next);
  }
}

let backoff = 1;

function connect() {
  const book = new BookKeeper();           // no state survives a reconnect
  const counts = {};
  let bufferedMax = 0;
  let nextWait = null;                     // set by handlers to override the backoff
  let stop = null;                         // set when reconnecting cannot help

  const sock = new WebSocket(WS_URL + encodeURIComponent(KEY), {
    perMessageDeflate: args.deflate,       // ws default is off; docs explain the trade-off
    maxPayload: 8 * 1024 * 1024,           // docs: 8 MB client limit
    handshakeTimeout: 15000,
  });

  sock.on("unexpected-response", (_req, res) => {
    let body = "";
    res.on("data", (c) => { body += c; });
    res.on("end", () => {
      if (res.statusCode === 401 || res.statusCode === 403) {
        stop = `HTTP ${res.statusCode} ${body.trim()} (${body.includes("plan_lacks_ws") ? "no active WebSocket add-on" : "check PINNRADAR_KEY"})`;
      } else if (res.statusCode === 429) {
        nextWait = Math.max(backoff, Number(res.headers["retry-after"]) || 60);
      }
      console.error(`handshake refused: HTTP ${res.statusCode} ${body.trim()}`);
      sock.terminate();
      scheduleReconnect();
    });
  });

  sock.on("open", () => {
    sock.send(JSON.stringify(subscribe));
    console.error(`connected; sent ${JSON.stringify(subscribe)}`);
  });

  const status = setInterval(() => {
    const frames = Object.entries(counts).map(([k, v]) => `${k}=${v}`).join(", ");
    console.log(`[status] ${book.records.size} records in book | frames: ${frames} | buffered_max_bytes=${bufferedMax}`);
  }, 10000);

  sock.on("message", (data) => {
    const frame = JSON.parse(data.toString());
    counts[frame.type] = (counts[frame.type] ?? 0) + 1;
    switch (frame.type) {
      case "ping":
        sock.send('{"type":"pong"}');
        bufferedMax = frame.buffered_max_bytes ?? 0;
        break;
      case "subscribed":
      case "unsubscribed":
        console.log(`${frame.type}: stream=${frame.stream} sport_id=${frame.sport_id}`);
        break;
      case "snapshot":
        book.apply(frame);
        backoff = 1;                       // a baseline arrived: the connection is healthy
        console.log(`snapshot ${frame.stream} sport ${frame.sport_id}` +
          `${frame.seq !== undefined ? ` part ${frame.seq}` : ""}${frame.final ? " (final)" : ""}: ` +
          `${(frame.events ?? []).length} events`);
        break;
      case "live":
        book.apply(frame);
        if (args.verbose) {
          const channel = String(frame.topic ?? "").split("/").pop();
          console.log(`live ${frame.op.padEnd(3)} id=${frame.rec?.id} ch=${channel} ` +
            `markets=${(frame.rec?.markets ?? []).length}${frame.mode ? ` mode=${frame.mode}` : ""}`);
        }
        break;
      case "prematch_markets":
        book.apply(frame);
        break;
      case "prematch_membership": {
        const added = frame.added ?? [];
        const removed = frame.removed ?? [];
        removed.forEach((id) => book.records.delete(id));
        if (added.length || removed.length) {
          console.log(`membership sport ${frame.sport_id}: +${added.length} -${removed.length}`);
        }
        break;
      }
      case "error":
        console.error(`server error: ${frame.code} ${frame.message ?? ""}`);
        if (FATAL.has(frame.code)) {
          stop = `fix the subscribe message (${frame.code})`;
          sock.close();
        } else if (frame.code === "backpressure") {
          // the sport was NOT subscribed; keep reading, then retry
          setTimeout(() => sock.readyState === WebSocket.OPEN && sock.send(JSON.stringify(subscribe)), 1000);
        }
        break;
      default:
        break;                             // prematch_matchups, prematch_ws: counted only
    }
  });

  sock.on("close", (code, reasonBuf) => {
    clearInterval(status);
    const reason = reasonBuf.toString();
    if (code) console.error(`socket closed: ${code} ${reason}`);
    if (reason.includes("evicted by newer connection")) {
      stop = "another connection on this account took over the socket (one WebSocket per account)";
    } else if (reason.startsWith("deregistered: ")) {
      nextWait = 0.5;                      // server-side fault: reconnect right away
      if (reason.includes("slow_consumer")) console.error("hint: your reader fell behind");
    }
    scheduleReconnect();
  });

  sock.on("error", (err) => {
    // handshake refusals are reported by the unexpected-response handler above
    if (!/Unexpected server response|closed before the connection/.test(String(err.message))) {
      console.error(`connection error: ${err.message}`);
    }
  });

  let scheduled = false;
  function scheduleReconnect() {
    if (scheduled) return;
    scheduled = true;
    clearInterval(status);
    if (stop) {
      console.error(`error: ${stop}`);
      process.exit(1);
    }
    const wait = nextWait ?? backoff;
    backoff = Math.min(30, Math.max(backoff, wait) * 2);
    console.error(`reconnecting in ${wait}s`);
    setTimeout(connect, wait * 1000);
  }
}

process.on("SIGINT", () => {
  console.error("\nstopped.");
  process.exit(0);
});

connect();
