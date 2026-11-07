// The scenarios compare.cjs runs on both pages: [name, actions, query, path]
// (path defaults to the home page, /).
// Each runs the same actions on the Python page and the React app.
const click = (sel) => async (p) => { await p.locator(sel).first().click(); };
// Items are picked by shape, not title, so the scenarios survive a fresh capture.
const open = (sel) => async (p) => { await p.locator(sel + ' > summary.row').first().click(); };
const GRAPH = '.card.items > details.item:has(.graph)';
const PLAIN = '.card.items > details.item:not(:has(.graph)):has(.inside .page):has(a.nwu)';
const TAGGED = '.card.items > details.item:has(summary .tag)';
const nth = (n) => async (p) => { await p.locator('.card.items > details.item > summary.row').nth(n).click(); };
const seq = (...fs) => async (p) => { for (const f of fs) { await f(p); await p.waitForTimeout(250); } };
module.exports = [
  ['home', async () => {}],
  ['open-graph-item', open(GRAPH)],
  ['open-plain-item', open(PLAIN)],
  ['open-several', seq(nth(1), nth(2), nth(3))],
  ['idle-open', click('details.idlefold > summary')],
  ['idle-item-open', seq(click('details.idlefold > summary'), async (p) => { await p.locator('.idlebox details.item > summary').nth(2).click(); })],
  ['chips-done-hidden-unopened', seq(click('#states button:has-text("Done")'), click('#states button:has-text("Hidden")'), click('#states button:has-text("Unopened")'))],
  ['chips-only-hidden', seq(click('#states button:has-text("Your turn")'), click('#states button:has-text("Agent working")'), click('#states button:has-text("Idle")'), click('#states button:has-text("Hidden")'))],
  ['chips-nothing', seq(click('#states button:has-text("Your turn")'), click('#states button:has-text("Agent working")'), click('#states button:has-text("Idle")'))],
  ['tag-chip', click('#tags button')],
  ['sort-result', async (p) => { await p.selectOption('#sort', 'result'); }],
  ['sort-untouched', async (p) => { await p.selectOption('#sort', 'untouched'); }],
  ['sort-spent', async (p) => { await p.selectOption('#sort', 'spent'); }],
  ['limit-6', async (p) => { await p.selectOption('#limit', '6'); }],
  ['limit-50', async (p) => { await p.selectOption('#limit', '50'); }],
  ['project-research', async (p) => { await p.selectOption('#proj', 'research'); }],
  ['find-kitchen', async (p) => { await p.fill('#filter', 'kitchen'); await p.waitForTimeout(500); }],
  ['find-nomatch', async (p) => { await p.fill('#filter', 'zzqqxx'); await p.waitForTimeout(500); }],
  ['find-picatrix', async (p) => { await p.fill('#filter', 'picatrix'); await p.waitForTimeout(500); }],
  // + Tag first: at phone width the open ⋮ menu covers the + Tag button (on both pages)
  ['rowmenu-and-tag', seq(open(PLAIN), click('details.item[open] details.addtag > summary'), click('details.item[open] details.rowmenu > summary'))],
  ['newwork-dialog', click('#newworkbtn')],
  ['newwork-folder', seq(click('#newworkbtn'), async (p) => { await p.locator('#workdir').pressSequentially('~/Projects/cad', { delay: 30 }); await p.waitForTimeout(600); })],
  ['newwork-folder-pick', seq(click('#newworkbtn'), click('.sugg button'))],
  ['newwork-skill', seq(click('#newworkbtn'), click('details.pick > summary'), async (p) => { await p.locator('#runq').pressSequentially('revsur', { delay: 30 }); await p.waitForTimeout(300); })],
  ['newwork-under-item', seq(open(PLAIN), click('details.item[open] a.nwu'))],
  ['newproject-open', click('details.newproject > summary')],
  // item actions, applied by the parity server to an in-memory store
  ['act-done', seq(open(PLAIN), click('details.item[open] button:has-text("Mark done")'))],
  ['act-done-then-done-chip', seq(open(PLAIN), click('details.item[open] button:has-text("Mark done")'), click('#states button:has-text("Done")'))],
  ['act-hide', seq(open(PLAIN), click('details.item[open] button:has-text("Hide from home")'))],
  ['act-unhide', seq(click('#states button:has-text("Hidden")'), async (p) => { await p.locator('details.item[data-state=hidden] > summary').first().click(); }, click('details.item[open] button:has-text("Show on home")'))],
  ['act-tag', seq(open(PLAIN), click('details.item[open] details.addtag > summary'), async (p) => { await p.locator('details.item[open] input[name=tag]:not([type=hidden])').fill('dinner'); await p.locator('details.item[open] input[name=tag]:not([type=hidden])').press('Enter'); })],
  ['act-untag', seq(open(TAGGED), click('details.item[open] .acts button:has-text("#")'))],
  ['act-archive', seq(open(PLAIN), click('details.item[open] details.rowmenu > summary'), click('details.item[open] details.rowmenu button'))],
  ['act-catch-me-up', seq(click('details.idlefold > summary'), async (p) => { await p.locator('.idlebox details.item > summary').first().click(); }, click('.idlebox details.item[open] .cmu button'))],
  ['act-start-work', seq(click('#newworkbtn'), async (p) => { await p.fill('#what', 'parity check'); }, click('dialog#newwork button[type=submit]'))],
  // links that set the view
  ['q-notice', async () => {}, '?notice=Task+submitted%3A+task-x'],
  ['q-parent', async () => {}, '?parent=task-you-re-generally-aware-o-20261101-152005-575b2d'],
  ['q-show-done', async () => {}, '?show=done'],
  ['q-show-all', async () => {}, '?show=all'],
  ['q-show-unopened', async () => {}, '?show=unopened'],
  ['q-sort-spent-limit-30', async () => {}, '?sort=spent&limit=30'],
  ['q-limit-7', async () => {}, '?limit=7'],
  ['q-tag', async () => {}, '?tag=stir-fry'],
  // keyboard in the folder field
  ['newwork-folder-keys', seq(click('#newworkbtn'), async (p) => { await p.locator('#workdir').pressSequentially('~/Projects/', { delay: 30 }); await p.waitForTimeout(500); await p.keyboard.press('ArrowDown'); await p.keyboard.press('ArrowDown'); })],
  // the other screens, each at its own path
  ['library', async () => {}, '', '/library'],
  ['costs', async () => {}, '', '/costs'],
  ['runs', async () => {}, '', '/runs'],
  ['runs-every-turn', async () => {}, '?view=turns', '/runs'],
  ['runs-open', async () => {}, '?view=open', '/runs'],
  ['runs-failed-by-cost', async () => {}, '?view=failed&sort=cost', '/runs'],
  ['runs-longest', async () => {}, '?view=turns&sort=longest', '/runs'],
  ['runs-search', async () => {}, '?q=fleet', '/runs'],
  ['runs-nothing', async () => {}, '?q=zzqqxx', '/runs'],
  ['runs-tab-click', click('p.meta a[href*="view=turns"]'), '', '/runs'],
  ['runs-pick-sort', async (p) => { await p.selectOption('#runfilter select[name=sort]', 'oldest'); await p.waitForTimeout(800); }, '', '/runs'],
  ['runs-pick-node', async (p) => { await p.selectOption('#runfilter select[name=node]', { index: 1 }); await p.waitForTimeout(800); }, '', '/runs'],
  ['runs-type-and-apply', async (p) => { await p.fill('#runfilter input[name=q]', 'cadre'); await p.press('#runfilter input[name=q]', 'Enter'); await p.waitForTimeout(800); }, '', '/runs'],
  ['tree', async () => {}, '', '/tree'],
  ['tree-open-a-root', click('section.card > .tnode a.ttitle'), '', '/tree'],
  ['tree-open-a-child', click('ul.tree li .tnode a.ttitle'), '', '/tree'],
  ['home-footer-to-tree', click('footer a[href="/tree"]')],
  ['home-footer-to-library', click('footer a[href="/library"]')],
  // the project page, for every project in the capture, and the manage (repair) page
  ['projectpage-blackboard', async () => {}, '', '/project/blackboard'],
  ['projectpage-cadre-runner', async () => {}, '', '/project/cadre-runner'],
  ['projectpage-hitl', async () => {}, '', '/project/hitl'],
  ['projectpage-home', async () => {}, '', '/project/home'],
  ['projectpage-juiz', async () => {}, '', '/project/juiz'],
  ['projectpage-picatrix', async () => {}, '', '/project/picatrix'],
  ['projectpage-projects', async () => {}, '', '/project/projects'],
  ['projectpage-research', async () => {}, '', '/project/research'],
  ['projectpage-review-surface', async () => {}, '', '/project/review-surface'],
  ['projectpage-shared-backend-smoke-test', async () => {}, '', '/project/shared-backend-smoke-test'],
  ['project-picker-open', click('details.pick > summary'), '', '/project/cadre-runner'],
  ['project-launch', async (p) => { await p.fill('#launch textarea[name=text]', 'parity check'); await p.click('#launch button:has-text("Launch")'); await p.waitForTimeout(1200); }, '', '/project/cadre-runner'],
  ['project-study-it', async (p) => { await p.fill('#launch textarea[name=text]', 'parity topic'); await p.click('#launch button[value=tutor]'); await p.waitForTimeout(1200); }, '', '/project/cadre-runner'],
  ['page-from-runs', click('#runs a.hist[href^="/page/"]'), '', '/runs'],
  ['page-from-every-turn', async (p) => { await p.locator('#runs a.hist[href^="/page/"]').nth(7).click(); }, '?view=turns', '/runs'],
  ['page-move', async (p) => { await p.locator('#runs a.hist[href^="/page/"]').first().click(); await p.waitForTimeout(1200); await p.click('form.repair button:has-text("Move")'); await p.waitForTimeout(1200); }, '', '/runs'],
  ['page-end-declined', async (p) => { await p.locator('#runs a.hist[href^="/page/"]').first().click(); await p.waitForTimeout(1200); const b = p.locator('form.repair button:has-text("End session")'); if (await b.isEnabled()) await b.click(); await p.waitForTimeout(800); }, '', '/runs'],
  ['newproject-from-home', async (p) => { await p.click('details.newproject > summary'); await p.fill('details.newproject input[name=name]', 'Parity'); await p.click('details.newproject button:has-text("Create")'); await p.waitForTimeout(1200); }],
];
