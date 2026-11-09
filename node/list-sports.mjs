#!/usr/bin/env node
// List PinnRadar sport ids, optionally with how many events each board holds.
//
// Sport ids are stable integers, so the table needs no request. With --count
// the script calls GET /kit/v1/markets once per sport (13 requests; 14 and 15
// are subsets of 10) and prints the number of events on the board.
//
// Usage (Node 20+, no dependencies for this file):
//   export PINNRADAR_KEY=your_key
//   node list-sports.mjs
//   node list-sports.mjs --count              # live boards
//   node list-sports.mjs --count --prematch   # upcoming fixtures
//   node list-sports.mjs --count --min-interval 0.1   # paid REST plans
//
// A Free key allows 20 requests per minute and 100 per day, so calls are spaced
// by --min-interval seconds (default 3.1). HTTP 429 is retried after Retry-After.

import { parseArgs } from "node:util";
import { setTimeout as sleep } from "node:timers/promises";

const BASE = process.env.PINNRADAR_BASE ?? "https://pinnradar.com";
const KEY = (process.env.PINNRADAR_KEY ?? "").trim();

const SPORTS = {
  1: "Soccer", 2: "Tennis", 3: "Basketball", 4: "Hockey", 5: "Football", 6: "Baseball",
  7: "Rugby (union and league)", 8: "MMA", 9: "Boxing", 10: "Other (volleyball + handball)",
  11: "Esports", 12: "Golf", 13: "Cricket", 14: "Volleyball (subset of 10)",
  15: "Handball (subset of 10)",
};

const { values: args } = parseArgs({
  options: {
    count: { type: "boolean", default: false },
    prematch: { type: "boolean", default: false },
    "min-interval": { type: "string", default: "3.1" },
  },
});

if (!KEY) {
  console.error("error: set PINNRADAR_KEY to your API key first.");
  console.error("  Get a free key at https://pinnradar.com/panel#signup");
  process.exit(2);
}

const HINTS = {
  missing_key: "no API key reached the server; check PINNRADAR_KEY",
  invalid_key: "unknown key (typo, deleted, or replaced by a regenerated key)",
  prematch_disabled: "the prematch feed is switched off right now",
};

let lastCall = 0;
const minGapMs = Number(args["min-interval"]) * 1000;

/** GET a JSON endpoint with pacing, 429 Retry-After handling and 5xx backoff. */
async function getJson(path, params) {
  const url = new URL(path, BASE);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));
  for (let attempt = 1; ; attempt++) {
    const gap = Date.now() - lastCall;
    if (gap < minGapMs) await sleep(minGapMs - gap);
    lastCall = Date.now();

    const resp = await fetch(url, { headers: { "x-portal-apikey": KEY } });
    if (resp.ok) return resp.json();

    const body = await resp.json().catch(() => ({}));
    if (resp.status === 429) {
      const waitS = Number(resp.headers.get("retry-after")) || (body.retry_after_ms ?? 5000) / 1000;
      if (waitS > 120) throw new Error(`rate limited (window=${body.window ?? "?"}); retry in ${Math.round(waitS)}s`);
      console.error(`[429] rate limited; waiting ${Math.ceil(waitS)}s`);
      await sleep(Math.ceil(waitS) * 1000);
      continue;
    }
    if (resp.status >= 500 && attempt < 5) {
      await sleep(2 ** attempt * 1000);
      continue;
    }
    const code = body.error ?? resp.statusText;
    throw new Error(`HTTP ${resp.status} ${code}${HINTS[code] ? `: ${HINTS[code]}` : ""}`);
  }
}

async function main() {
  if (!args.count) {
    console.log(" id  sport");
    for (const [id, name] of Object.entries(SPORTS)) console.log(`${id.padStart(3)}  ${name}`);
    console.log("\nTip: add --count to see how many events each board holds right now.");
    return;
  }
  const board = args.prematch ? "prematch" : "live";
  console.log(` id  ${"sport".padEnd(32)} ${(board + " events").padStart(15)}`);
  let total = 0;
  for (const [id, name] of Object.entries(SPORTS)) {
    if (id === "14" || id === "15") {
      console.log(`${id.padStart(3)}  ${name.padEnd(32)} ${"(see 10)".padStart(15)}`);
      continue;
    }
    const body = await getJson("/kit/v1/markets", { sport_id: id, event_type: board });
    const n = (body.events ?? []).length;
    total += n;
    console.log(`${id.padStart(3)}  ${name.padEnd(32)} ${String(n).padStart(15)}`);
  }
  console.log(`\n${total} ${board} events in total`);
}

main().catch((err) => {
  console.error(`error: ${err.message}`);
  process.exit(1);
});
