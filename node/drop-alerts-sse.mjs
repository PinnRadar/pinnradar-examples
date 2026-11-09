#!/usr/bin/env node
// Follow the dropping-odds SSE stream and print each drop as it happens.
//
// Streams (Server-Sent Events over one long-lived HTTP connection):
//   live:      GET /odds-drop?min_drop=N
//   prematch:  GET /odds-drop-prematch?min_drop=N&recheck=S   (--prematch)
// Needs a plan with SSE (Drops, Edge or Scale); a Free or REST key is refused
// with plan_lacks_sse and the script exits.
//
// Usage (Node 20+, no dependencies: built-in fetch and a small SSE parser):
//   export PINNRADAR_KEY=your_key
//   node drop-alerts-sse.mjs
//   node drop-alerts-sse.mjs --min-drop 8 --sport 1 --sport 3
//   node drop-alerts-sse.mjs --prematch --recheck 30
//
// Frames: the first is {"type":"connected","id":"..."}; every later frame is
// a JSON array of drops. One SSE connection per account and stream: a second
// copy on the same key closes the first. Reconnects back off exponentially
// (1 s up to 60 s) and honour Retry-After on rate_limited. There is no replay
// of drops missed while disconnected; GET /api/drops?max_age_sec=... can
// backfill a gap.

import { parseArgs } from "node:util";
import { setTimeout as sleep } from "node:timers/promises";

const BASE = process.env.PINNRADAR_BASE ?? "https://pinnradar.com";
const KEY = (process.env.PINNRADAR_KEY ?? "").trim();

const { values: args } = parseArgs({
  options: {
    prematch: { type: "boolean", default: false },
    "min-drop": { type: "string", default: "5" },
    recheck: { type: "string", default: "0" },
    sport: { type: "string", multiple: true, default: [] },
  },
});

if (!KEY) {
  console.error("error: set PINNRADAR_KEY to your API key first.");
  console.error("  Get a free key at https://pinnradar.com/panel#signup");
  process.exit(2);
}

class StopError extends Error {}

const sports = new Set(args.sport.map(Number));
const path = args.prematch ? "/odds-drop-prematch" : "/odds-drop";
const url = new URL(path, BASE);
url.searchParams.set("min_drop", String(Math.max(1, Number(args["min-drop"]))));
if (args.prematch && Number(args.recheck) > 0) url.searchParams.set("recheck", args.recheck);

const fmt = (x) => (typeof x === "number" ? x.toFixed(3) : "-");

function printDrop(d) {
  if (sports.size && !sports.has(d.sport_id)) return;
  const pct = d.from_price ? ((d.from_price - d.to_price) / d.from_price) * 100 : null;
  const edge = d.nvp ? d.to_price / d.nvp - 1 : null;
  const when = d.alerted ? new Date(d.alerted * 1000).toLocaleTimeString() : "--:--:--";
  let market = d.sect ?? "?";
  if (d.point != null) market += ` ${d.point}`;
  if (d.period) market += ` (period ${d.period})`;
  let state = "";
  if (d.live?.status) {
    state = ` [${d.live.status}${d.live.score ? ` ${d.live.score.home}-${d.live.score.away}` : ""}]`;
  }
  let line = `          ${d.league ?? ""} | ${market} | ${d.outcome}: ${fmt(d.from_price)} -> ${fmt(d.to_price)}`;
  if (pct != null) line += `  (-${pct.toFixed(1)}%)`;
  if (edge != null) line += `  nvp ${fmt(d.nvp)}  edge ${(edge * 100).toFixed(1)}%`;
  if (d.rechecked_ms) line += `  held ${Math.round(d.rechecked_ms / 1000)}s`;
  console.log(`${when}  ${String(d.sport ?? "?").padEnd(10)} ${d.home} v ${d.away}${state}\n${line}`);
}

/** One connection. Resolves when the stream ends; throws StopError when retrying is pointless. */
async function readStream(signal, onConnected) {
  const resp = await fetch(url, {
    headers: { "x-portal-apikey": KEY, accept: "text/event-stream" },
    signal,
  });
  if (resp.status === 401 || resp.status === 403) {
    const body = await resp.json().catch(() => ({}));
    throw new StopError(`HTTP ${resp.status} ${body.error ?? resp.statusText}`);
  }
  if (resp.status === 429) {
    const err = new Error("HTTP 429 rate_limited");
    err.retryAfter = Number(resp.headers.get("retry-after")) || 60;
    throw err;
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

  const decoder = new TextDecoder();
  let pending = "";
  for await (const chunk of resp.body) {
    // normalise CRLF on the whole buffer so a \r\n split across chunks is handled
    pending = (pending + decoder.decode(chunk, { stream: true })).replace(/\r\n/g, "\n");
    let cut;
    while ((cut = pending.indexOf("\n\n")) !== -1) {
      const block = pending.slice(0, cut);
      pending = pending.slice(cut + 2);
      const data = block
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).replace(/^ /, ""))
        .join("\n");
      if (!data) continue;
      let frame;
      try {
        frame = JSON.parse(data);
      } catch {
        continue;
      }
      if (Array.isArray(frame)) {
        frame.forEach(printDrop);
      } else if (frame.type === "connected") {
        onConnected();
        console.error(`connected (id ${frame.id}); waiting for drops`);
      } else if (frame.type === "error") {
        if (frame.error === "plan_lacks_sse") {
          throw new StopError("your plan has no SSE streams (Drops, Edge and Scale do)");
        }
        const err = new Error(`server error frame: ${frame.error}`);
        if (frame.error === "rate_limited") err.retryAfter = Number(resp.headers.get("retry-after")) || 60;
        throw err;
      }
    }
  }
}

async function main() {
  const controller = new AbortController();
  process.on("SIGINT", () => {
    controller.abort();
    console.error("\nstopped.");
    process.exit(0);
  });

  console.error(`connecting to ${path}?${url.searchParams} ... (Ctrl-C to stop)`);
  let backoff = 1;
  for (;;) {
    let retryAfter = 0;
    try {
      // a "connected" frame means the connection is healthy: reset the backoff
      await readStream(controller.signal, () => { backoff = 1; });
      console.error("[sse] stream ended");
    } catch (err) {
      if (err instanceof StopError) throw err;
      retryAfter = err.retryAfter ?? 0;
      console.error(`[sse] ${err.message}`);
    }
    const wait = Math.max(backoff, retryAfter);
    console.error(`[sse] reconnecting in ${wait}s`);
    await sleep(wait * 1000);
    backoff = Math.min(60, Math.max(backoff, wait) * 2);
  }
}

main().catch((err) => {
  console.error(`error: ${err.message}`);
  process.exit(1);
});
