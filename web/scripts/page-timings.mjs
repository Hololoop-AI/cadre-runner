// How long the fleet and a review page take to show their content in a real
// browser over a throttled link (Chromium's network emulation: added
// latency per request, capped down/up throughput).
//
//   node scripts/page-timings.mjs [--base http://fedora-1.tail2057e0.ts.net:8183]
//        [--latency 80] [--mbit 10] [--runs 5] [--session /session/<key>]
//        [--only fleet|classic|review] [--warm]
//
// Prints one line per page: the median and every run of "content visible"
// (the first work item on the fleet, the artifact's body inside the review
// page's frame), the requests made, the bytes on the wire, and the slowest
// last run's requests as a waterfall (start → end, ms from the first request).
// Cold cache each run, unless --warm.
import { chromium } from 'playwright-core';
import { readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const arg = (name, dflt) => {
  const i = process.argv.indexOf('--' + name);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const BASE = arg('base', 'http://fedora-1.tail2057e0.ts.net:8183');
const LATENCY = Number(arg('latency', 80));
const MBIT = Number(arg('mbit', 10));
const RUNS = Number(arg('runs', 5));
const ONLY = arg('only', '');
// --warm: a second visit — the page is loaded once untimed in the same browser
// context, so what it may keep (cache headers permitting) is kept
const WARM = process.argv.includes('--warm');

function chromiumPath() {
  const root = join(homedir(), '.cache', 'ms-playwright');
  const dir = readdirSync(root).filter((d) => /^chromium-\d+$/.test(d)).sort().pop();
  return join(root, dir, 'chrome-linux64', 'chrome');
}

async function firstSession(browser) {
  const page = await browser.newPage();
  const res = await page.request.get(BASE + '/api/fleet');
  const fleet = await res.json();
  await page.close();
  return (fleet.snap.surfaces.find((s) => s.path && s.artifact) || {}).path;
}

async function measure(browser, url, visible) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: !WARM });
  const bps = (MBIT * 1e6) / 8;
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false, latency: LATENCY, downloadThroughput: bps, uploadThroughput: bps,
  });
  if (WARM) {
    await page.goto(url, { waitUntil: 'commit' });
    await visible(page);
    await page.waitForLoadState('networkidle').catch(() => {});
    await page.goto('about:blank');
  }
  const reqs = new Map();
  cdp.on('Network.requestWillBeSent', (e) => reqs.set(e.requestId, { url: e.request.url, t0: e.timestamp }));
  cdp.on('Network.responseReceived', (e) => {
    const r = reqs.get(e.requestId);
    if (r) Object.assign(r, { enc: e.response.headers['content-encoding'] || e.response.headers['Content-Encoding'] || '' });
  });
  cdp.on('Network.loadingFinished', (e) => {
    const r = reqs.get(e.requestId);
    if (r) Object.assign(r, { t1: e.timestamp, bytes: e.encodedDataLength });
  });
  const t0 = Date.now();
  await page.goto(url, { waitUntil: 'commit' });
  await visible(page);
  const shown = Date.now() - t0;
  await page.waitForLoadState('networkidle').catch(() => {});
  const settled = Date.now() - t0;
  const list = [...reqs.values()];
  await ctx.close();
  return { shown, settled, list };
}

const fmt = (ms) => (ms / 1000).toFixed(2) + 's';
const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];

async function report(browser, name, url, visible) {
  const runs = [];
  for (let i = 0; i < RUNS; i++) runs.push(await measure(browser, url, visible));
  const last = runs[runs.length - 1];
  const kb = last.list.reduce((s, r) => s + (r.bytes || 0), 0) / 1024;
  console.log(`${name}: visible ${fmt(median(runs.map((r) => r.shown)))} median `
    + `[${runs.map((r) => fmt(r.shown)).join(' ')}], settled ${fmt(median(runs.map((r) => r.settled)))}, `
    + `${last.list.length} requests, ${kb.toFixed(0)} KB on the wire`);
  // the last run's waterfall: when each request started and ended, from the first
  const start = Math.min(...last.list.map((r) => r.t0));
  for (const r of last.list.filter((r) => r.t1 && !r.url.startsWith('data:')).sort((a, b) => a.t0 - b.t0)) {
    const path = r.url.replace(/^https?:\/\/[^/]+/, '').slice(0, 70);
    const ms = (t) => ((t - start) * 1000).toFixed(0).padStart(5);
    console.log(`    ${ms(r.t0)} → ${ms(r.t1)} ms  ${String(Math.round((r.bytes || 0) / 1024)).padStart(4)} KB  ${(r.enc || '-').padEnd(4)}  ${path}`);
  }
}

const browser = await chromium.launch({ executablePath: chromiumPath() });
console.log(`${BASE}, ${LATENCY} ms latency, ${MBIT} Mbit/s, ${RUNS} ${WARM ? 'second-visit' : 'cold'} runs each`);
const fleetShown = (page) => page.waitForSelector('details.item', { timeout: 30000 });
if (!ONLY || ONLY === 'fleet') await report(browser, 'fleet (React, /)', BASE + '/', fleetShown);
if (!ONLY || ONLY === 'classic') await report(browser, 'fleet (Python, /classic)', BASE + '/classic', fleetShown);
if (!ONLY || ONLY === 'review') {
  const session = arg('session', '') || await firstSession(browser);
  // the artifact frame is replaced as the page loads, so poll whichever is current
  await report(browser, `review page (${session})`, BASE + session, async (page) => {
    for (const t = Date.now(); Date.now() - t < 30000; await page.waitForTimeout(10)) {
      const frame = page.frames().find((f) => f.url().includes('/artifact/'));
      if (frame && await frame.evaluate(() => !!document.querySelector('body *')).catch(() => false)) return;
    }
    throw new Error('the artifact never showed');
  });
}
await browser.close();
