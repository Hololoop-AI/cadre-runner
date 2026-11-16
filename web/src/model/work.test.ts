import { describe, expect, it } from 'vitest';
import { cardFacts, LOOSE_FRESH_S, workRows, type Fleet, type Item, type Kanban } from '.';

const NOW = 10_000_000;
const card = (id: string, column: string, runs: string[] = [], moved = NOW - 100) =>
  ({ id, title: id, note: '', column, item: '', runs, by: 'driver', created_at: 1, moved_at: moved });
const item = (key: string, state: string, touched: number) => ({ key, state, touched, returned: 0, working: state === 'working' }) as unknown as Item;

const BOARD: Kanban = {
  board: 'dev', columns: ['backlog', 'ready', 'doing', 'done'],
  cards: [card('quiet', 'doing', [], NOW - 500), card('busy', 'doing', ['t-busy']), card('later', 'backlog'), card('shipped', 'done')],
};
const FLEET = { tree: { 't-busy': { title: 'busy run', working: true, since: NOW - 10 } } } as unknown as Fleet;

describe('workRows', () => {
  it('puts active cards and loose runs in one list: needs you, then working, then quiet', () => {
    const loose = [item('mine', 'your-turn', NOW - 3600), item('idle', 'idle', NOW - 60)];
    const w = workRows(BOARD, FLEET, loose, NOW);
    expect(w.rows.map((r) => r.key)).toEqual(['mine', 'card:busy', 'idle', 'card:quiet']);
    expect([w.needs, w.working]).toEqual([1, 1]);
  });

  it('folds a loose run quiet for more than a week below the list, but never one an agent is on', () => {
    const old = NOW - LOOSE_FRESH_S - 1;
    const w = workRows(null, FLEET, [item('stale', 'your-turn', old), item('agent', 'working', old)], NOW);
    expect(w.rows.map((r) => r.key)).toEqual(['agent']);
    expect(w.older.map((i) => i.key)).toEqual(['stale']);
  });

  it('ages a loose run by the time its row shows: its newest result before a later touch', () => {
    const viewed = { ...item('viewed', 'your-turn', NOW - 60), returned: NOW - LOOSE_FRESH_S - 1 } as Item;
    expect(workRows(null, FLEET, [viewed], NOW).older.map((i) => i.key)).toEqual(['viewed']);
  });

  it('leaves backlog and done cards out', () => {
    expect(workRows(BOARD, FLEET, [], NOW).rows.every((r) => r.kind === 'card' && r.card.column === 'doing')).toBe(true);
  });

  it('sorts and filters the cards as it does the runs: by project, state and newest result', () => {
    const fleet = { tree: { 't-busy': { title: 'b', working: true, since: NOW - 10 }, 't-q': { title: 'q', since: NOW - 900 } },
      returned: { 't-q': NOW - 20 } } as unknown as Fleet;
    const board: Kanban = { ...BOARD, cards: [card('quiet', 'doing', ['t-q'], NOW - 500), card('busy', 'doing', ['t-busy'])] };
    const items = new Map([['t-q', { key: 't-q', project: 'site', spent: 2 } as Item], ['t-busy', { key: 't-busy', project: 'app', spent: 1 } as Item]]);
    const keys = (v: object) => workRows(board, fleet, [], NOW, items, v).rows.map((r) => r.key);
    expect(keys({ proj: 'site' })).toEqual(['card:quiet']);
    expect(keys({ show: ['working'] })).toEqual(['card:busy']);
    expect(keys({ sort: 'result' })).toEqual(['card:quiet', 'card:busy']);
    expect(keys({ sort: 'spent' })).toEqual(['card:quiet', 'card:busy']);
  });
});

describe('cardFacts', () => {
  it('names the projects its runs ran in, most runs first, adds their cost and takes the newest result', () => {
    const items = new Map([['a', { project: 'x', spent: 1 }], ['b', { project: 'y', spent: 2 }], ['c', { project: 'y', spent: 0 }]] as [string, Item][]);
    expect(cardFacts(['a', 'b', 'c', 'gone'], items, [5, 9, 0])).toEqual({ projects: ['y', 'x'], spent: 3, returned: 9 });
  });
});
