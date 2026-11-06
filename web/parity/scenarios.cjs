// The scenarios compare.cjs runs on both pages: [name, actions, query].
// Each runs the same actions on the Python page and the React app.
const click = (sel) => async (p) => { await p.locator(sel).first().click(); };
const summary = (title) => async (p) => {
  await p.locator('details.item > summary', { hasText: title }).first().click();
};
const seq = (...fs) => async (p) => { for (const f of fs) { await f(p); await p.waitForTimeout(250); } };
module.exports = [
  ['home', async () => {}],
  ['open-graph-item', summary('Faster Cadre sessions')],
  ['open-plain-item', summary('Chicken thigh dinner')],
  ['open-several', seq(summary('Green dots for unopened'), summary('Agent core template'), summary('Ballot picks'))],
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
  ['rowmenu-and-tag', seq(summary('Chicken thigh dinner'), click('details.item[open] details.rowmenu > summary'), click('details.item[open] details.addtag > summary'))],
  ['newwork-dialog', click('#newworkbtn')],
  ['newwork-folder', seq(click('#newworkbtn'), async (p) => { await p.locator('#workdir').pressSequentially('~/Projects/cad', { delay: 30 }); await p.waitForTimeout(600); })],
  ['newwork-folder-pick', seq(click('#newworkbtn'), click('.sugg button'))],
  ['newwork-skill', seq(click('#newworkbtn'), click('details.pick > summary'), async (p) => { await p.locator('#runq').pressSequentially('revsur', { delay: 30 }); await p.waitForTimeout(300); })],
  ['newwork-under-item', seq(summary('Chicken thigh dinner'), click('details.item[open] a.nwu'))],
  ['newproject-open', click('details.newproject > summary')],
  // item actions, applied by the parity server to an in-memory store
  ['act-done', seq(summary('Chicken thigh dinner'), click('details.item[open] button:has-text("Mark done")'))],
  ['act-done-then-done-chip', seq(summary('Chicken thigh dinner'), click('details.item[open] button:has-text("Mark done")'), click('#states button:has-text("Done")'))],
  ['act-hide', seq(summary('Chicken thigh dinner'), click('details.item[open] button:has-text("Hide from home")'))],
  ['act-unhide', seq(click('#states button:has-text("Hidden")'), async (p) => { await p.locator('details.item[data-state=hidden] > summary').first().click(); }, click('details.item[open] button:has-text("Show on home")'))],
  ['act-tag', seq(summary('Chicken thigh dinner'), click('details.item[open] details.addtag > summary'), async (p) => { await p.locator('details.item[open] input[name=tag]:not([type=hidden])').fill('dinner'); await p.locator('details.item[open] input[name=tag]:not([type=hidden])').press('Enter'); })],
  ['act-untag', seq(summary('Chicken thigh dinner'), click('details.item[open] button:has-text("#stir-fry")'))],
  ['act-archive', seq(summary('Chicken thigh dinner'), click('details.item[open] details.rowmenu > summary'), click('details.item[open] details.rowmenu button'))],
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
];
