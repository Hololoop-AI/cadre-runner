import { describe, expect, it } from 'vitest';
import { folderPicks } from './picker';
import { effectiveGroup, fleetGroups, groupBadges, groupTag, inGroup, itemGroups, OTHER } from './groups';
import { buildHome } from './home';
import type { Fleet, Project } from './payload';
import { layout, loadSel, serverView } from './view';

const NOW = 1_800_000_000;
const P = (id: string, name: string, group: string | null = null, dirs: string[] = [], archived = false): Project =>
  ({ id, name, group, dirs, archived });
const PROJS: Record<string, Project> = {
  dev: P('dev', 'Dev'),
  everyday: P('everyday', 'Everyday'),
  'cadre-runner': P('cadre-runner', 'cadre-runner', 'dev', ['/w/cadre-runner']),
  hitl: P('hitl', 'hitl', 'cadre-runner', ['/w/hitl']),
  research: P('research', 'research', 'everyday', ['/home/r']),
  lonely: P('lonely', 'Lonely', null, ['/w/lonely']),
  gone: P('gone', 'Gone', null, [], true),
  orphan: P('orphan', 'Orphan', 'gone'),
};

describe('fleetGroups', () => {
  it('is every live project another live project belongs to, by name', () => {
    expect(fleetGroups(PROJS).map((g) => g.id)).toEqual(['cadre-runner', 'dev', 'everyday']);
  });

  it('is empty while no project names a group, so no sidebar is drawn', () => {
    expect(fleetGroups({ lonely: PROJS.lonely })).toEqual([]);
  });
});

describe('itemGroups', () => {
  const groups = fleetGroups(PROJS);
  it('climbs the project chain: hitl is in cadre-runner and in Dev', () => {
    expect(itemGroups({ project: 'hitl', tags: [] }, PROJS, groups)).toEqual(['cadre-runner', 'dev']);
    expect(itemGroups({ project: 'research', tags: [] }, PROJS, groups)).toEqual(['everyday']);
  });

  it('lets a tag naming a group win over the project', () => {
    expect(itemGroups({ project: 'hitl', tags: ['everyday'] }, PROJS, groups)).toEqual(['everyday']);
    expect(itemGroups({ project: '', tags: ['cooking', 'Dev'] }, PROJS, groups)).toEqual(['dev']);
  });

  it('puts work with no project, or a project in no group, in none (Other)', () => {
    expect(itemGroups({ project: '', tags: ['cooking'] }, PROJS, groups)).toEqual([]);
    expect(itemGroups({ project: 'lonely', tags: [] }, PROJS, groups)).toEqual([]);
    expect(itemGroups({ project: 'not-a-project', tags: [] }, PROJS, groups)).toEqual([]);
    expect(inGroup([], OTHER)).toBe(true);
    expect(inGroup(['dev'], OTHER)).toBe(false);
    expect(inGroup(['dev'], '')).toBe(true);
  });

  it('survives a loop in the records', () => {
    const loop = { a: P('a', 'A', 'b'), b: P('b', 'B', 'a') };
    expect(itemGroups({ project: 'a', tags: [] }, loop, fleetGroups(loop))).toEqual(['a', 'b']);
  });
});

describe('the kept group', () => {
  const groups = fleetGroups(PROJS);
  it('falls back to All when the group is gone or none exist', () => {
    expect(effectiveGroup('dev', groups)).toBe('dev');
    expect(effectiveGroup(OTHER, groups)).toBe(OTHER);
    expect(effectiveGroup('gone', groups)).toBe('');
    expect(effectiveGroup('dev', [])).toBe('');
  });

  it('comes from ?group= over the kept view, and names the New work tag', () => {
    expect(loadSel('?group=everyday', null).group).toBe('everyday');
    expect(loadSel('', null).group).toBe('');
    expect(groupTag('everyday', groups)).toBe('Everyday');
    expect(groupTag(OTHER, groups)).toBe('');
  });
});

describe('the home page by group', () => {
  const page = (task: string, cwd: string, opened: number) =>
    ({ path: `/session/${task.padEnd(16, '0')}`, task, cwd, opened, title: task });
  const fleet: Fleet = {
    projs: PROJS,
    snap: { surfaces: [page('t-runner', '/w/cadre-runner/x', NOW - 30), page('t-recipe', '/w/cadre-runner', NOW - 20),
      page('t-shop', '/home/r', NOW - 10), page('t-loose', '/tmp', NOW - 5)] },
    items: { 't-recipe': { tags: ['Everyday'], done_at: null, hidden_at: null } },
  } as unknown as Fleet;
  const home = buildHome(fleet, serverView(''), NOW, '');
  const shown = (group: string) => layout(home.items, loadSel('', null), {
    q: '', proj: '', sort: '', limit: 15, text: (it) => it.title,
    grouped: group ? (it) => inGroup(home.member[it.key] || [], group) : undefined,
  }).list.map((it) => it.key).sort();

  it('files each item by tag, then by its folder\'s project', () => {
    expect(home.member).toMatchObject({ 't-runner': ['cadre-runner', 'dev'], 't-recipe': ['everyday'], 't-shop': ['everyday'], 't-loose': [] });
  });

  it('shows only the chosen group, and everything under All', () => {
    expect(shown('dev')).toEqual(['t-runner']);
    expect(shown('everyday')).toEqual(['t-recipe', 't-shop']);
    expect(shown(OTHER)).toEqual(['t-loose']);
    expect(shown('')).toEqual(['t-loose', 't-recipe', 't-runner', 't-shop']);
  });

  it('applies the home limit and the chips within the group', () => {
    const one = (group: string) => layout(home.items, loadSel('', null), {
      q: '', proj: '', sort: '', limit: 1, text: (it) => it.title,
      grouped: (it) => inGroup(home.member[it.key] || [], group),
    });
    const ev = one('everyday');
    expect(ev.list.map((it) => it.key)).toEqual(['t-shop']);
    expect(ev.idle.map((it) => it.key)).toEqual(['t-recipe']);
    expect(ev.count).toMatchObject({ 'your-turn': 1, idle: 1 });
    expect(one('dev').list.map((it) => it.key)).toEqual(['t-runner']);
  });

  it('counts unopened items per entry for the badges', () => {
    const items = home.items.map((it) => ({ ...it, unopened: it.key !== 't-runner' }));
    expect(groupBadges(items, home.member, home.groups)).toEqual({ '': 3, [OTHER]: 1, 'cadre-runner': 0, dev: 0, everyday: 2 });
  });
});

describe('a group project with no folder', () => {
  it('offers no folder pick, as statusd does (it read "undefined" before)', () => {
    expect(folderPicks({ dev: P('dev', 'Dev'), r: P('r', 'r', 'dev', ['/home/me/r']) }, '/home/me')).toEqual(['~/r']);
  });
});
