// Drive the Python fleet page and the React app through the same scenarios
// against a parity server (serve.py), then compare them: each page's DOM,
// serialized the same way, and a full-page screenshot of each.
//
//   node web/parity/compare.cjs --fixture web/parity/fixture.json --out /tmp/parity/desk \
//     [--base http://127.0.0.1:8197] [--width 1100] [--only home,act-tag]
//
// Prints one line per scenario: IDENTICAL or DIFFERS, with py.txt/react.txt
// and py.png/react.png left in <out>/<scenario>/ to diff and look at.
// Chromium: $CHROME_PATH, else the newest one Playwright has installed.
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const os = require('os');

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const base = arg('base', 'http://127.0.0.1:8197');
const out = arg('out', path.join(os.tmpdir(), 'cadre-parity'));
const width = +arg('width', '1100');
const only = arg('only', '') ? arg('only').split(',') : null;
const now = JSON.parse(fs.readFileSync(arg('fixture', path.join(__dirname, 'fixture.json')), 'utf8')).now;

function chrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const root = path.join(os.homedir(), '.cache', 'ms-playwright');
  const dirs = fs.existsSync(root) ? fs.readdirSync(root).filter((d) => /^chromium-\d+$/.test(d)).sort() : [];
  for (const d of dirs.reverse()) {
    const exe = path.join(root, d, 'chrome-linux64', 'chrome');
    if (fs.existsSync(exe)) return exe;
  }
  return undefined;
}

// Runs in the page: .wrap and the New work dialog as indented lines. Scripts
// are left out; adjacent text is merged; attributes are sorted; what the two
// renderers express differently but mean the same (a select's chosen option,
// an input's typed value, boolean attributes, style spacing) is normalized.
// Elements marked data-beyond-parity are React's deliberate additions and
// are left out.
function serialize() {
  const SKIP = new Set(['SCRIPT', 'NOSCRIPT']);
  const BOOL = new Set(['hidden', 'open', 'required', 'disabled']);
  const lines = [];
  let text = null;
  const flush = () => {
    if (text === null) return;
    const t = text[0].replace(/\s+/g, ' ').trim();
    if (t) lines.push('  '.repeat(text[1]) + JSON.stringify(t));
    text = null;
  };
  const typed = (n) => (n.tagName === 'INPUT' && n.type !== 'hidden') || n.tagName === 'TEXTAREA';
  const walk = (n, d) => {
    if (n.nodeType === 3) {
      text = text ? [text[0] + n.textContent, text[1]] : [n.textContent, d];
      return;
    }
    flush();
    if (n.nodeType !== 1 || SKIP.has(n.tagName)) return;
    // what the React app adds on purpose (the item graph's history toggle) is not the Python page's
    if (n.hasAttribute('data-beyond-parity') || n.hasAttribute('data-beyond-parity-row')) return;
    const attrs = [];
    for (const a of n.attributes) {
      let v = a.value;
      if (a.name === 'selected' || (a.name === 'value' && typed(n))) continue;
      if (a.name === 'style') v = v.replace(/\s+/g, '').replace(/;$/, '');
      if (BOOL.has(a.name)) v = '';
      if (a.name === 'class') v = v.split(/\s+/).filter(Boolean).join(' ');
      attrs.push(`${a.name}=${JSON.stringify(v)}`);
    }
    if (n.tagName === 'SELECT') attrs.push(`value=${JSON.stringify(n.value)}`);
    if (n.tagName === 'DETAILS') attrs.push(`isopen=${n.open}`);
    if (typed(n)) attrs.push(`prop-value=${JSON.stringify(n.value)}`);
    attrs.sort();
    lines.push('  '.repeat(d) + `<${n.tagName.toLowerCase()}${attrs.length ? ' ' + attrs.join(' ') : ''}>`);
    for (const c of n.childNodes) walk(c, d + 1);
    flush();
  };
  const wrap = document.querySelector('.wrap');
  if (!wrap) return `NO .wrap AT ${location.href}\n${document.body.innerText.slice(0, 300)}`;
  walk(wrap, 0);
  const dialog = document.querySelector('dialog#newwork');
  if (dialog) walk(dialog, 0);
  return lines.join('\n') + '\n';
}

// a scenario's path: the Python page at it, the React app at /app + it
const PAGES = { py: (path) => path, react: (path) => '/app' + path };

(async () => {
  const b = await chromium.launch({ executablePath: chrome() });
  const rows = [];
  const failed = new Set();
  for (const [scen, act, query = '', where = '/'] of require('./scenarios.cjs')) {
    if (only && !only.includes(scen)) continue;
    const dir = path.join(out, scen);
    fs.mkdirSync(dir, { recursive: true });
    for (const [name, at] of Object.entries(PAGES)) {
      const page = at(where);
      await fetch(base + '/parity/reset');
      const ctx = await b.newContext({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
      const p = await ctx.newPage();
      // the parity server's clock is stopped at the fixture's instant; so is the page's
      await p.clock.setFixedTime(new Date(Math.ceil(now * 1000)));
      p.on('pageerror', (e) => console.error(scen, name, 'pageerror', e.message));
      await p.goto(base + page + query, { waitUntil: 'load' });
      await p.waitForTimeout(1200);
      try {
        await act(p);
      } catch (e) {
        // a scenario whose action failed did not test what it names: say so in the result
        failed.add(scen);
        console.error(scen, name, 'action failed', e.message.split('\n')[0]);
      }
      await p.waitForTimeout(900);
      // park the mouse: a hover highlight left by the last click is not the page
      await p.mouse.move(0, 0);
      fs.writeFileSync(path.join(dir, `${name}.txt`), await p.evaluate(serialize));
      await p.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true });
      await ctx.close();
    }
    const [a, r] = ['py', 'react'].map((n) => fs.readFileSync(path.join(dir, `${n}.txt`), 'utf8'));
    rows.push([scen, failed.has(scen) ? 'ACTION FAILED' : a === r ? 'IDENTICAL' : 'DIFFERS', `${a.split('\n').length} lines`]);
  }
  await b.close();
  for (const row of rows) console.log(row.join('\t'));
  process.exit(rows.every((r) => r[1] === 'IDENTICAL') ? 0 : 1);
})();
