import { describe, expect, it } from 'vitest';
import { collapse, findPages } from './conversations';
import { pyFixed, relTime, taskLabel, usd, viewHref } from './format';
import { buildItemGraph } from './graph';
import { assignStates, fleetItems, itemFacts, type Item } from './items';
import { effectiveView, layout, loadSel, serverView, tagChips } from './view';
import { clean, kidsOf } from './tree';
import { findEntries, projectHint } from './picker';
import { pyRound, runsHref, runsSel, runsView, treeRoots } from './screens';

const NOW = 1_800_000_000;

describe('format, in statusd.py words', () => {
  it('says times, money, task ids and page links the same way', () => {
    expect(relTime(NOW - 5, NOW)).toBe('5s ago');
    expect(relTime(NOW - 7200, NOW)).toBe('2h ago');
    expect(relTime(0, NOW)).toBe('');
    expect(usd(0.414)).toBe('$0.41');
    expect(usd(1176.4)).toBe('$1,176');
    expect(taskLabel('task-fix-the-thing-20260925-124025-badfc9')).toBe('fix the thing');
    expect(viewHref('/session/abc')).toBe('/view/abc');
  });
});

describe('collapse', () => {
  it('folds a dialogue\'s rounds into one row, newest round as the head', () => {
    const rows = collapse(
      [
        { path: '/session/aaaaaaaaaaaaaaa1', task: 't1', opened: 10, title: 'round 1' },
        { path: '/session/aaaaaaaaaaaaaaa2', task: 't1', opened: 20, title: 'round 2' },
        { path: '/session/bbbbbbbbbbbbbbb1', task: 't2', opened: 15, title: 'other', cwd: '/w/repo' },
      ],
    );
    expect(rows.map((r) => r.title)).toEqual(['round 2', 'other']);
    expect(rows[0].earlier!.map((e) => e.title)).toEqual(['round 1']);
    expect(rows[1].project).toBe('repo');
  });

  it('keeps a side thread off its dialogue\'s rounds and carries it on the row', () => {
    const rows = collapse([
      { path: '/session/aaaaaaaaaaaaaaa1', task: 't1', opened: 10, title: 'round 1', kind: 'task' },
      { path: '/session/ccccccccccccccc1', task: 't1', opened: 30, title: 'Thread: why', kind: 'thread' },
      { path: '/session/ccccccccccccccc2', task: 't1', opened: 20, title: 'Learn: old', kind: 'study' },
      { path: '/session/ccccccccccccccc3', task: 'gone', opened: 40, title: 'Thread: lone', kind: 'thread' },
    ]);
    expect(rows.map((r) => r.title)).toEqual(['Thread: lone', 'round 1']);
    expect(rows[1].earlier).toEqual([]);
    expect(rows[1].threads!.map((e) => e.title)).toEqual(['Thread: why', 'Learn: old']);
    expect(collapse(rows)[1].threads!.length).toBe(2);
  });

  it('finds closed pages too, title hits first', () => {
    const hits = findPages('kitchen', [], [], {}, {
      k1: { title: 'Kitchen magnets', updated: 5 },
      k2: { title: 'Other', file: '/x/kitchen.html', updated: 9 },
    });
    expect(hits.map((h) => h.key)).toEqual(['k1', 'k2']);
  });
});

function items(n: number, working: number[] = []): Item[] {
  const surfaces = Array.from({ length: n }, (_, i) => ({
    path: `/session/k${i}`, task: `t${i}`, opened: NOW - i * 100, title: `item ${i}`, kind: 'task', artifact: 'a',
  }));
  const tree = clean({});
  const its = fleetItems({ surfaces, tree, projs: {} });
  const doing = Object.fromEntries(working.map((i) => [`t${i}`, 'Bash']));
  itemFacts(its, { tree, kids: {}, statuses: {}, doing, returned: {}, spent: {}, now: NOW, running: [], endings: {}, bare: {} });
  return its;
}

describe('item states', () => {
  it('idles only what falls past the home limit, never an item an agent works on', () => {
    const its = assignStates(items(5, [4]), {}, 2);
    // the limit counts the working item too: room for one quiet item
    expect(its.map((it) => it.state)).toEqual(['your-turn', 'idle', 'idle', 'idle', 'working']);
  });

  it('takes done and hidden from the store', () => {
    const its = assignStates(items(2), { t0: { done_at: 1 }, t1: { hidden_at: 1, tags: ['x'] } });
    expect(its.map((it) => [it.state, it.tags])).toEqual([['done', []], ['hidden', ['x']]]);
  });

  it('lays out the list the way the page script does: idle folds, done is off the home', () => {
    const its = assignStates(items(4), { t3: { done_at: 1 } }, 2);
    const sel = loadSel('', null);
    const placed = layout(its, sel, { q: '', proj: '', sort: 'needs', limit: 2, text: (it) => it.title });
    expect(placed.list.map((it) => it.key)).toEqual(['t0', 't1']);
    expect(placed.idle.map((it) => it.key)).toEqual(['t2']);
    expect(placed.off.map((it) => it.key)).toEqual(['t3']);
    expect(placed.count).toMatchObject({ 'your-turn': 2, idle: 1, done: 1 });
    const found = layout(its, sel, { q: 'item 3', proj: '', sort: 'needs', limit: 2, text: (it) => it.title });
    expect(found.list.map((it) => it.key)).toEqual(['t3']);
  });

  it('reads ?show= from the link over the kept view', () => {
    expect(loadSel('?show=done', null).show).toEqual(['done']);
    expect(loadSel('?show=unopened', null).unopened).toBe(true);
  });
});

describe('item graph', () => {
  it('draws a settled branch as one row with what it holds', () => {
    const tree = clean({
      root: { title: 'root', since: 1 },
      a: { parent: 'root', since: 2 }, a1: { parent: 'a', since: 3 }, a2: { parent: 'a', since: 4 },
      b: { parent: 'root', since: 5, working: true },
    });
    const g = buildItemGraph('root', [], tree, kidsOf(tree), {}, NOW, {}, {});
    expect(g.rows.map((r) => [r.task, r.depth, r.hidden, r.state])).toEqual([
      ['root', 0, 0, 'done'], ['a', 1, 2, 'done'], ['b', 1, 0, 'work'],
    ]);
    expect(g.rows[1].lines).toEqual(['M7,0 V7 Q7,12 12,12 H17', 'M7,12 V24']);
  });
});

describe('New work', () => {
  const projs = { cadre: { id: 'cadre', name: 'Cadre', dirs: ['/home/me/Projects/cadre/'] } };
  it('says which project a typed folder files under', () => {
    expect(projectHint('~/Projects/cadre/runner', '/home/me', projs)).toBe('Files under project “Cadre” (from the folder).');
    expect(projectHint('/tmp/x', '/home/me', projs)).toMatch(/^No project owns this folder yet/);
  });

  it('ranks a skill by its name before its description', () => {
    const lib = [
      { v: 'skill:a', i: '/arboreus-teach', k: 'skill', d: 'review surface research' },
      { v: 'skill:b', i: '/review-surface', k: 'skill', d: '' },
    ];
    expect(findEntries(lib, 'revsur').map((e) => e.i)).toEqual(['/review-surface']);
  });
});

describe('Python edge cases the port keeps', () => {
  it('rounds an exact half to even, as Python formats money and data-spent', () => {
    expect(pyFixed(0.125, 2)).toBe('0.12');
    expect(pyFixed(0.135, 2)).toBe((0.135).toFixed(2)); // not an exact half in binary: no change
    expect(usd(100.5)).toBe('$100');
    expect(usd(101.5)).toBe('$102');
  });

  it('reads ?limit= the way int() does, and refuses prototype keys as a sort', () => {
    expect(serverView('?limit=0').limit).toBe(1);
    expect(serverView('?limit=1.5').limit).toBe(15);
    expect(serverView('?limit=1_0').limit).toBe(10);
    expect(serverView('?limit=%207%20').limit).toBe(7);
    expect(serverView('?sort=constructor').sort).toBe('needs');
    expect(serverView('?sort=spent').sort).toBe('spent');
  });

  it('sets the controls as the page script does', () => {
    const sel = { ...loadSel('', null), sort: 'toString', limit: 0, proj: 'gone' };
    expect(effectiveView(sel, serverView('?sort=result&limit=30'), [['a', 'A']])).toEqual({ sort: 'result', limit: 30, proj: '' });
    expect(tagChips(['a'], { ...sel, tag: 'kept' })).toEqual(['a', 'kept']);
  });
});

describe('opening a graph row', () => {
  const tree = clean({ root: { title: 'root', since: 1 }, a: { parent: 'root', since: 2 }, a1: { parent: 'a', since: 3 } });
  const page = {
    path: '/session/k2', task: 'root', opened: 30, title: 'round three', kind: 'task', artifact: 'x',
    earlier: [{ key: 'k1', path: '/session/k1', title: 'round two', opened: 20 },
      { key: 'k0', path: '/session/k0', title: 'round one', opened: 10 }],
  };
  const closed = buildItemGraph('root', [page], tree, kidsOf(tree), {}, NOW, {}, {});
  const opened = buildItemGraph('root', [page], tree, kidsOf(tree), {}, NOW, {}, {}, new Set(['root', 'a']));

  it('says what is behind a row without drawing it', () => {
    expect(closed.rows.map((r) => [r.task, r.hidden, r.history, r.open])).toEqual([
      ['root', 0, 2, false], ['a', 1, 1, false],
    ]);
  });

  it('hangs the earlier rounds under the page, newest first, and unfolds a settled branch', () => {
    expect(opened.rows.map((r) => [r.task, r.depth, r.round?.title ?? ''])).toEqual([
      ['root', 0, ''], ['round:k1', 1, 'round two'], ['round:k0', 1, 'round one'], ['a', 1, ''], ['a1', 2, ''],
    ]);
    expect(opened.rows.find((r) => r.task === 'a')).toMatchObject({ hidden: 0, history: 1, open: true });
  });

  it('counts side threads in the history and hangs them after the rounds', () => {
    const withThread = { ...page, threads: [{ key: 't9', path: '/session/t9', title: 'Thread: why', opened: 40 }] };
    const g = buildItemGraph('root', [withThread], tree, kidsOf(tree), {}, NOW, {}, {}, new Set(['root']));
    expect(g.rows[0]).toMatchObject({ task: 'root', history: 3, page: withThread });
    expect(g.rows.slice(1, 4).map((r) => [r.task, r.round?.title ?? r.thread?.title])).toEqual([
      ['round:k1', 'round two'], ['round:k0', 'round one'], ['thread:t9', 'Thread: why'],
    ]);
  });
});

describe('the other screens', () => {
  it('rounds minutes the way Python does', () => {
    expect([0.5, 1.5, 2.5, 2.4, 2.6].map(pyRound)).toEqual([0, 2, 2, 2, 3]);
  });

  it('reads the runs query as statusd does and links a view the same way', () => {
    const sel = runsSel('?view=bogus&sort=cost&q=%20fleet%20');
    expect(sel).toEqual({ view: 'bogus', sort: 'cost', project: '', node: '', q: 'fleet' });
    expect(runsView(sel)).toBe('latest');
    expect(runsHref(sel, { view: 'turns' })).toBe('/runs?view=turns&sort=cost&q=fleet');
    expect(runsHref(runsSel(''))).toBe('/runs');
  });

  it('lists as trees only sessions that launched others and were launched by none, newest first', () => {
    const { roots } = treeRoots({
      old: { since: 1 }, a: { parent: 'old', since: 2 }, young: { since: 9 }, b: { parent: 'young', since: 10 }, alone: { since: 5 },
    });
    expect(roots).toEqual(['young', 'old']);
  });
});
