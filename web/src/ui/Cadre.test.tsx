import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildHome, cadreCards, needsYou, inGroup, OTHER, serverView, type Fleet, type Nav, type Project } from '../model';
import { App } from './App';

// Each cadre owns its runs and opens its own app (/c/<id>); Hololoop (/)
// sees a card per cadre and no runs; the shell's tabs, side column and phone
// menu come from the cadre's navigation list (GET /api/nav).

const NOW = Date.now() / 1000;
const P = (id: string, name: string, group: string | null = null, dirs: string[] = []): Project =>
  ({ id, name, group, dirs, archived: false });
const page = (task: string, cwd: string, ago: number) =>
  ({ path: `/session/${task.padEnd(16, '0')}`, task, cwd, opened: NOW - ago, title: `${task} page` });
const FLEET: Fleet = {
  projs: { dev: P('dev', 'Dev'), kitchen: P('kitchen', 'Kitchen'), 'cadre-runner': P('cadre-runner', 'cadre-runner', 'dev', ['/w/cr']) },
  // t-kit works in Dev's folder but was launched in Kitchen: the record, not the folder, decides
  snap: { surfaces: [page('t-dev', '/w/cr', 30), page('t-kit', '/w/cr', 20), page('t-loose', '/tmp', 10)] },
  cadres: { 't-dev': 'dev', 't-kit': 'kitchen' },
  cadre_week_usd: { dev: 4.5, kitchen: 1.25 },
  running: [], slots: 4, week_usd: 5.75,
} as unknown as Fleet;
const NAV: Nav = {
  cadre: { id: 'dev', name: 'Dev' },
  screens: [{ id: 'home', label: 'Home', href: '/c/dev' }, { id: 'board', label: 'Board', href: '/c/dev/board' },
    { id: 'agents', label: 'Agents', href: '/c/dev/agents' }, { id: 'settings', label: 'Settings', href: '/c/dev/settings' }],
  batches: [], pinned: [{ label: 'Cadre design', href: '/view/abc' }],
  cadres: [{ id: 'dev', name: 'Dev' }, { id: 'kitchen', name: 'Kitchen' }, { id: OTHER, name: 'Other' }],
};

describe('the cadre model', () => {
  const home = buildHome(FLEET, serverView(''), NOW, '');
  const shown = (c: string) => home.items.filter((it) => inGroup(home.member[it.key] || [], c)).map((it) => it.key);

  it("files each item under the cadre that owns its run, not its folder's group", () => {
    expect(shown('dev')).toEqual(['t-dev']);
    expect(shown('kitchen')).toEqual(['t-kit']);
    expect(shown(OTHER)).toEqual(['t-loose']);
  });

  it('gives Hololoop one card per cadre with its waiting, running and week cost', () => {
    const cards = cadreCards(home.items, home.member, home.groups, FLEET);
    expect(cards.map((c) => [c.id, c.waiting, c.weekUsd])).toEqual(
      [['dev', 1, 4.5], ['kitchen', 1, 1.25], [OTHER, 1, 0]]);
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

function serving(path: string) {
  window.history.pushState(null, '', path);
  const asked: string[] = [];
  vi.stubGlobal('EventSource', NoStream);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const u = new URL(url);
    asked.push(u.pathname + u.search);
    const body = u.pathname === '/api/fleet' ? FLEET
      : u.pathname === '/api/nav' ? NAV
        : u.pathname === '/api/kanban' ? { board: 'dev', columns: ['backlog', 'ready', 'doing', 'done'], cards: [] }
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
  it('shows only its own runs, with the board leading and Needs you beside it', async () => {
    const asked = serving('/c/dev');
    await act(async () => { render(<App />); });
    const list = document.getElementById('fleet')!;
    expect(list.textContent).toContain('t-dev page');
    expect(list.textContent).not.toContain('t-kit page');
    expect(list.textContent).not.toContain('t-loose page');
    const needs = screen.getByRole('complementary', { name: 'Needs you and running now' });
    expect(within(needs).getAllByRole('link').map((a) => a.textContent)).toEqual(['t-dev page']);
    expect(document.querySelector('.cmain .kactive')).toBeTruthy();
    expect(asked).toContain('/api/nav?cadre=dev');
  });

  it("draws its header tabs and side column from the cadre's navigation, Settings among them", async () => {
    serving('/c/dev');
    await act(async () => { render(<App />); });
    const tabs = screen.getAllByRole('navigation', { name: 'Dev screens' })[0];
    expect(within(tabs).getAllByRole('link').map((a) => [a.textContent, a.getAttribute('href')])).toEqual(
      [['Home', '/c/dev'], ['Board', '/c/dev/board'], ['Agents', '/c/dev/agents'], ['Settings', '/c/dev/settings']]);
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
    serving('/c/kitchen/board');
    await act(async () => { render(<App />); });
    expect(screen.getAllByRole('navigation', { name: 'Dev screens' }).length).toBeGreaterThan(0);
    expect(screen.getByText(/This cadre has no board yet/)).toBeTruthy();
  });
});

describe('Hololoop', () => {
  it('shows a card per cadre and none of their runs', async () => {
    serving('/');
    await act(async () => { render(<App />); });
    const cards = [...document.querySelectorAll('a.ccard')];
    expect(cards.map((a) => a.getAttribute('href'))).toEqual(['/c/dev', '/c/kitchen', '/c/other']);
    expect(cards[0].textContent).toContain('1 needs you');
    expect(cards[0].textContent).toContain('$4.50 this week');
    expect(document.body.textContent).not.toContain('t-dev page');
    expect(document.getElementById('fleet')).toBeNull();
  });
});
