import { describe, expect, it } from 'vitest';
import { isDriversTurn, needsYou, keyOf, elapsed } from './fleet';
import type { Fleet, SessionStatus, Surface } from './types';

const st = (o: Partial<SessionStatus> = {}): SessionStatus => ({
  status: 'open', presence: 'waiting', pending_prompts: 0, updated_at: null,
  last_agent_reply_at: null, viewed_at: null, unseen: false, ...o,
});
const sf = (key: string, o: Partial<Surface> = {}): Surface => ({
  kind: 'task', path: `/session/${key}`, opened: 100, project: 'p', title: null,
  task: `task-${key}`, cwd: '/w', artifact: '/a.html', ...o,
});
function fleet(surfaces: Surface[], statuses: Record<string, SessionStatus>, extra: Partial<Fleet> = {}): Fleet {
  return {
    machine: 'm', week_usd: 0, slots: 4, snap: { surfaces }, graph: [[], {}, {}],
    statuses, recent: [], projs: {}, running: [], items: {}, ...extra,
  };
}

describe('isDriversTurn', () => {
  it('is true for an open page whose agent is waiting with nothing queued', () => {
    expect(isDriversTurn(st())).toBe(true);
  });
  it.each([
    ['agent working', st({ presence: 'working' })],
    ['feedback queued for the agent', st({ pending_prompts: 2 })],
    ['session ended', st({ status: 'ended' })],
    ['no status known', undefined],
  ])('is false when %s', (_, s) => {
    expect(isDriversTurn(s)).toBe(false);
  });
});

describe('needsYou', () => {
  it('keeps only pages where it is the driver’s turn', () => {
    const f = fleet([sf('a'), sf('b'), sf('c'), sf('d')], {
      a: st(), b: st({ presence: 'working' }), c: st({ pending_prompts: 1 }),
    });
    expect(needsYou(f).map((r) => r.key)).toEqual(['a']);
  });

  it('drops superseded rounds, DECIDED pages, busy tasks and hidden/done items', () => {
    const f = fleet(
      [sf('old', { task: 't1' }), sf('new', { task: 't2' }), sf('dec', { title: 'd9 (DECIDED: x)' }),
       sf('busy'), sf('hid'), sf('done')],
      { old: st(), new: st(), dec: st(), busy: st(), hid: st(), done: st() },
      {
        graph: [[{ type: 'supersedes', from: 'new', to: 'old' }], {}, {}],
        running: [{ task: 'task-busy', title: '', doing: '', page: '', item_title: '', folder: '',
                    started: 1, deadline: 0, waiting: false }],
        items: { 'task-hid': { tags: [], done_at: null, hidden_at: 5 },
                 'task-done': { tags: [], done_at: 5, hidden_at: null } },
      },
    );
    expect(needsYou(f).map((r) => r.key)).toEqual(['new']);
  });

  it('a queued-but-unslotted launch does not hide its page', () => {
    const f = fleet([sf('a')], { a: st() }, {
      running: [{ task: 'task-a', title: '', doing: '', page: '', item_title: '', folder: '',
                  started: 1, deadline: 0, waiting: true }],
    });
    expect(needsYou(f)).toHaveLength(1);
  });

  it('shows one row per task (its newest page) and puts unseen first, then newest', () => {
    const f = fleet(
      [sf('a1', { task: 'T', opened: 10 }), sf('a2', { task: 'T', opened: 20 }),
       sf('b', { opened: 50 }), sf('c', { opened: 5 })],
      { a1: st(), a2: st(), b: st(), c: st({ unseen: true }) },
    );
    expect(needsYou(f).map((r) => r.key)).toEqual(['c', 'b', 'a2']);
  });

  it('titles from the surface, else the page list, and marks replies', () => {
    const f = fleet([sf('a')], { a: st({ last_agent_reply_at: '2026-11-05T06:00:00Z' }) },
      { graph: [[], {}, { a: { title: 'From pages' } }] });
    const [row] = needsYou(f);
    expect(row.title).toBe('From pages');
    expect(row.replied).toBe(true);
  });
});

describe('helpers', () => {
  it('keyOf takes the last path segment', () => {
    expect(keyOf('/session/abc')).toBe('abc');
  });
  it('elapsed formats seconds through days', () => {
    expect(elapsed(0, 45_000)).toBe('45s');
    expect(elapsed(0, 3_900_000)).toBe('1h 5m');
    expect(elapsed(0, 90_000_000)).toBe('1d 1h');
  });
});
