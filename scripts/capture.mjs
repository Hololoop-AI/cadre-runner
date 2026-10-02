// Screenshot + short video of a Cadre fleet page. usage: node capture.mjs <url> <outdir> <label>
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('/home/juiz/.npm/_npx/e41f203b7505f1fb/node_modules/playwright');
const [url, out, label] = process.argv.slice(2);
const browser = await chromium.launch({ args: ['--no-sandbox'] });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 },
  recordVideo: { dir: out, size: { width: 1280, height: 860 } } });
const page = await ctx.newPage();
const resp = await page.goto(url, { waitUntil: 'load', timeout: 20000 }).catch(e => ({ status: () => 'ERR ' + e.message }));
console.log(`${label}: GET ${url} -> ${resp.status()}`);
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/${label}.png` });
await page.mouse.wheel(0, 700); await page.waitForTimeout(1200);
await page.screenshot({ path: `${out}/${label}-scrolled.png` });
const v = page.video(); await ctx.close(); await browser.close();
const fs = await import('fs'); fs.renameSync(await v.path(), `${out}/${label}.webm`);
console.log(`${label}: wrote ${label}.png, ${label}-scrolled.png, ${label}.webm`);
