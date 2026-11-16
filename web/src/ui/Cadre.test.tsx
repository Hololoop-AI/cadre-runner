import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildHome, cadreCards, HOLO, needsYou, inGroup, OTHER, serverView, type Fleet, type Kanban, type Nav, type Project } from '../model';
import { App } from './App';

// Each cadre owns its runs and opens its own app (/c/<id>); Holo (/)
// sees a card per cadre and its own runs; the shell's tabs, side column and phone
// menu come from the cadre's navigation list (GET /api/nav).

const NOW = Date.now() / 1000;
const P = (id: string, name: string, group: string | null = null, dirs: string[] = []): Project =>
  ({ id, name, group, dirs, archived: false });
const page = (task: string, cwd: string, ago: number) =>
  ({ path: `/session/${task.padEnd(16, '0')}`, task, cwd, opened: NOW - ago, title: `${task} page` });
const FLEET: Fleet = {
  projs: { dev: P('dev', 'Dev'), kitchen: P('kitchen', 'Kitchen'), 'cadre-runner': P('cadre-runner', 'cadre-runner', 'dev', ['/w/cr']) },
  // t-kit works in Dev's folder but was launched in Kitchen: the record, not the folder, decides
  snap: { surfaces: [page('t-dev', '/w/cr', 30), page('t-kit', '/w/cr', 20), page('t-loose', '/tmp', 10),
    page('t-card', '/w/cr', 40), page('t-meta', '/tmp', 50)] },
  cadres: { 't-dev': 'dev', 't-kit': 'kitchen', 't-card': 'dev', 't-meta': HOLO },
  cadre_week_usd: { dev: 4.5, kitchen: 1.25 },
  running: [], slots: 4, week_usd: 5.75,
} as unknown as Fleet;
const NAV: Nav = {
  cadre: { id: 'dev', name: 'Dev' },
  screens: [{ id: 'home', label: 'Home', href: '/c/dev' }, { id: 'kanban', label: 'Kanban', href: '/c/dev/kanban' },
    { id: 'blackboard', label: 'Blackboard', href: '/c/dev/blackboard' }, { id: 'records', label: 'Records', href: '/c/dev/records' },
    { id: 'agents', label: 'Agents', href: '/c/dev/agents' }, { id: 'settings', label: 'Settings', href: '/c/dev/settings' }],
  batches: [], pinned: [{ label: 'Cadre design', href: '/view/abc' }],
  cadres: [{ id: 'dev', name: 'Dev' }, { id: 'kitchen', name: 'Kitchen' }, { id: OTHER, name: 'Other' }],
  settings: { project: 'dev', folders: [], members: [{ id: 'cadre-runner', name: 'cadre-runner' }], nav: [] },
};
const HOLO_NAV: Nav = { ...NAV, cadre: { id: HOLO, name: 'Holo' },
  screens: [{ id: 'home', label: 'Home', href: '/' }, { id: 'blackboard', label: 'Blackboard', href: '/c/holo/blackboard' },
    { id: 'settings', label: 'Settings', href: '/c/holo/settings' }], settings: { project: '', folders: [], members: [], nav: [] } };
const KCARD = (id: string, title: string, column: string, runs: string[] = []) =>
  ({ id, title, note: '', column, item: '', runs, by: 'driver', created_at: 1, moved_at: 1 });
const BOARD: Kanban = { board: 'dev', columns: ['backlog', 'ready', 'doing', 'done'],
  cards: [KCARD('aaaaaa', 'Cadre slice 2', 'doing', ['t-card']), KCARD('bbbbbb', 'Light theme', 'backlog')] };
const EVENT = { seq: 7, id: 'ev7', ts: NOW - 60, namespace: 'tasks', topic: 'dialogue', key: 'task-x', kind: 'command',
  provenance: { agent: 'implement' }, payload: { target: 'handoff', node: 'task' }, correlation_id: null, visible_after: null };

describe('the cadre model', () => {
  const home = buildHome(FLEET, serverView(''), NOW, '');
  const shown = (c: string) => home.items.filter((it) => inGroup(home.member[it.key] || [], c)).map((it) => it.key);

  it("files each item under the cadre that owns its run, not its folder's group", () => {
    expect(shown('dev')).toEqual(['t-dev', 't-card']);
    expect(shown('kitchen')).toEqual(['t-kit']);
    expect(shown(OTHER)).toEqual(['t-loose']);
    expect(shown(HOLO)).toEqual(['t-meta']);
  });

  it('gives Holo one card per cadre with its waiting, running and week cost', () => {
    const cards = cadreCards(home.items, home.member, home.groups, FLEET);
    expect(cards.map((c) => [c.id, c.waiting, c.weekUsd])).toEqual(
      [['dev', 2, 4.5], ['kitchen', 1, 1.25], [OTHER, 1, 0]]);
  });

  it("lists Needs you from the cadre's own items only, newest first", () => {
    expect(needsYou(home.items, (it) => inGroup(home.member[it.key] || [], 'kitchen')).map((it) => it.key)).toEqual(['t-kit']);
  });
});

class NoStream {
  addEventListener() {}
  close() {}
  onerror: (() => void) | null = null;
}

function serving(path: string, fleet: Fleet = FLEET) {
  window.history.pushState(null, '', path);
  const asked: string[] = [];
  vi.stubGlobal('EventSource', NoStream);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const u = new URL(url);
    asked.push(u.pathname + u.search);
    const body = u.pathname === '/api/fleet' ? fleet
      : u.pathname === '/api/nav' ? (u.searchParams.get('cadre') === 'holo' ? HOLO_NAV : NAV)
        : u.pathname === '/api/records' ? { records: [{ id: 'r1', cadre: 'dev', type: 'adr', name: 'cadres-own-runs.html', path: '/d/r1.html',
          bytes: 9, sha256: 'x', note: 'why', by: 'driver', created_at: NOW - 60, href: '/open?file=/d/r1.html' }] }
        : u.pathname === '/api/blackboard' ? { events: [EVENT] }
        : u.pathname === '/api/kanban' ? BOARD
          : u.pathname === '/api/backends' ? { backends: [] }
            : u.pathname === '/api/newwork' ? { home: '/home/me', library: [] } : {};
    return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
  }));
  return asked;
}

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState(null, '', '/');
});

describe('a cadre app', () => {
  it('shows only its own runs: those done for a card under the card, the rest as one-off work', async () => {
    const asked = serving('/c/dev');
    await act(async () => { render(<App />); });
    const work = screen.getByRole('region', { name: 'Work on the board' });
    const card = work.querySelector('details.wcard[data-card=aaaaaa]') as HTMLDetailsElement;
    expect(card.open).toBe(false); // closed: nothing on it came back unopened
    expect(card.querySelector('summary')!.textContent).toContain('Cadre slice 2');
    expect(card.querySelector('summary')!.textContent).toContain('1 run · 1 your turn');
    expect(within(card).getByText('t-card page')).toBeTruthy();
    expect(work.querySelector('.wbacklog a')!.textContent).toBe('Backlog · 1');
    expect(work.querySelector('details.wcard[data-card=bbbbbb]')).toBeNull(); // the Backlog is on the Kanban tab, not home
    const list = document.getElementById('fleet')!;
    expect(list.textContent).toContain('t-dev page');
    expect(list.textContent).not.toContain('t-card page');
    expect(list.textContent).not.toContain('t-kit page');
    expect(list.textContent).not.toContain('t-loose page');
    expect(screen.getByText('One-off work (on no card)')).toBeTruthy();
    const needs = screen.getByRole('complementary', { name: 'Needs you and running now' });
    expect(within(needs).getAllByRole('link').map((a) => a.textContent)).toEqual(['t-dev page', 't-card page']);
    expect(asked).toContain('/api/nav?cadre=dev');
  });

  it('opens a card on the Kanban tab to its runs, drawn as the Work list\'s graph, and starts work on it there', async () => {
    const asked = serving('/c/dev/kanban');
    const posts: [string, string][] = [];
    const real = globalThis.fetch as unknown as (u: string, o?: RequestInit) => Promise<Response>;
    vi.stubGlobal('fetch', vi.fn(async (url: string, o?: RequestInit) => {
      if (o?.method === 'POST') {
        posts.push([new URL(url).pathname, String(o.body)]);
        return new Response(JSON.stringify({ notice: 'Task submitted.' }), { headers: { 'Content-Type': 'application/json' } });
      }
      return real(url, o);
    }));
    await act(async () => { render(<App />); });
    await act(async () => { screen.getByRole('button', { name: '▸ 1 run · 1 your turn' }).click(); });
    const card = document.querySelector('section.kopen details.wcard[data-card=aaaaaa]') as HTMLDetailsElement;
    expect(card.classList.contains('item')).toBe(true);
    expect(card.querySelector('.wrun[data-run=t-card] .graph svg circle')).toBeTruthy();
    // an empty backlog card opens the same way, with its start box already open
    await act(async () => { screen.getByRole('button', { name: '▸ start work' }).click(); });
    expect(document.querySelector('section.kopen details.wcard[data-card=aaaaaa]')).toBeNull(); // one card open at a time
    const empty = document.querySelector('section.kopen details.wcard[data-card=bbbbbb]') as HTMLDetailsElement;
    expect(empty.textContent).toContain('Nothing has run for this card yet');
    const box = within(empty).getByRole('textbox', { name: 'What should the agent do for Light theme?' }) as HTMLTextAreaElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
      set.call(box, 'Draft the light theme');
      box.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => { within(empty).getByRole('button', { name: 'Start' }).click(); });
    expect(posts.length).toBe(1);
    const [path, body] = posts[0];
    const sent = new URLSearchParams(body);
    expect([path, sent.get('text'), sent.get('card'), sent.get('cadre')]).toEqual(['/tasks', 'Draft the light theme', 'bbbbbb', 'dev']);
    expect(asked).toContain('/api/kanban');
  });

  it('opens a card on its own only when a run on it came back and is not opened yet', async () => {
    const fresh = { ...FLEET, statuses: { [`t-card`.padEnd(16, '0')]: { unseen: true } } } as unknown as Fleet;
    serving('/c/dev', fresh);
    await act(async () => { render(<App />); });
    const card = document.querySelector('details.wcard[data-card=aaaaaa]') as HTMLDetailsElement;
    expect(card.open).toBe(true);
    expect(card.querySelector('summary')!.textContent).toContain('1 new');
  });

  it("draws its header tabs and side column from the cadre's navigation, Settings among them", async () => {
    serving('/c/dev');
    await act(async () => { render(<App />); });
    const tabs = screen.getAllByRole('navigation', { name: 'Dev screens' })[0];
    expect(within(tabs).getAllByRole('link').map((a) => [a.textContent, a.getAttribute('href')])).toEqual(
      [['Home', '/c/dev'], ['Kanban', '/c/dev/kanban'], ['Blackboard', '/c/dev/blackboard'], ['Records', '/c/dev/records'], ['Agents', '/c/dev/agents'],
        ['Settings', '/c/dev/settings']]);
    expect(within(tabs).getByText('Home').getAttribute('aria-current')).toBe('page');
    const side = screen.getByRole('complementary', { name: 'Dev: batches and pinned pages' });
    expect(within(side).getByText('Cadre design').getAttribute('href')).toBe('/view/abc');
    expect(within(side).getByText(/Dev settings/).getAttribute('href')).toBe('/c/dev/settings');
  });

  it('puts the rail, the screens and settings behind one menu button for a phone', async () => {
    serving('/c/dev');
    await act(async () => { render(<App />); });
    const button = screen.getByRole('button', { name: /^Menu:/ });
    expect(document.getElementById('cdrawer')).toBeNull();
    await act(async () => { button.click(); });
    const drawer = document.getElementById('cdrawer')!;
    expect(within(drawer).getByText('Settings').getAttribute('href')).toBe('/c/dev/settings');
    expect(document.querySelector('nav.groups.open')).toBeTruthy();
  });

  it('keeps the shell on its other screens, with Settings reachable from each', async () => {
    serving('/c/kitchen/kanban');
    await act(async () => { render(<App />); });
    expect(screen.getAllByRole('navigation', { name: 'Dev screens' }).length).toBeGreaterThan(0);
    expect(screen.getByText(/This cadre has no kanban board/)).toBeTruthy();
  });

  it('keeps the kanban board and the blackboard apart: the Blackboard tab lists events', async () => {
    serving('/c/dev/blackboard');
    await act(async () => { render(<App />); });
    const table = document.querySelector('table.bboard')!;
    expect(table.textContent).toContain('tasks/dialogue/task-x');
    expect(table.textContent).toContain('target=handoff');
    expect(document.querySelector('.kanban')).toBeNull();
  });

  it("shows the cadre's setup on Settings, not its runs", async () => {
    serving('/c/dev/settings');
    await act(async () => { render(<App />); });
    const st = document.querySelector('section.csettings')!;
    expect(st.textContent).toContain('How Dev is set up');
    expect(within(st as HTMLElement).getByText('cadre-runner').getAttribute('href')).toBe('/project/cadre-runner');
    expect(document.getElementById('fleet')).toBeNull();
    expect(document.body.textContent).not.toContain('t-dev page');
  });
});

describe('Holo', () => {
  it('is a cadre too: a card per cadre, then its own runs and none of theirs', async () => {
    const asked = serving('/');
    await act(async () => { render(<App />); });
    const cards = [...document.querySelectorAll('a.ccard')];
    expect(cards.map((a) => a.getAttribute('href'))).toEqual(['/c/dev', '/c/kitchen', '/c/other']);
    expect(cards[0].textContent).toContain('2 need you');
    expect(cards[0].textContent).toContain('$4.50 this week');
    const list = document.getElementById('fleet')!;
    expect(list.textContent).toContain('t-meta page');
    expect(list.textContent).not.toContain('t-dev page');
    expect(asked).toContain('/api/nav?cadre=holo');
    expect((document.querySelector('#newwork input[name=cadre]') as HTMLInputElement).value).toBe('holo');
  });

  it("links its header's board to the blackboard when it has no kanban board", async () => {
    serving('/');
    await act(async () => { render(<App />); });
    expect(screen.getByRole('link', { name: 'blackboard' }).getAttribute('href')).toBe('/c/holo/blackboard');
  });
});

describe('records', () => {
  it('lists a cadre\'s pages by type, each opening as a review page', async () => {
    serving('/c/dev/records');
    await act(async () => { render(<App />); });
    const link = screen.getByRole('link', { name: 'cadres-own-runs' });
    expect(link.getAttribute('href')).toBe('/open?file=/d/r1.html');
    expect(document.querySelector('section.rtype h3')!.textContent).toContain('adr');
  });
});
