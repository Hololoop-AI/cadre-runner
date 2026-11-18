import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildHome, inGroup, serverView, type Deck, type Fleet, type Nav, type Project } from '../model';
import { App } from './App';

// Learn, the study cadre: a study session is Learn's and also shows in the
// cadre that asked; Learn's app has a Review tab and a home panel from /api/deck.

const NOW = Date.now() / 1000;
const P = (id: string, name: string, group: string | null = null, dirs: string[] = []): Project =>
  ({ id, name, group, dirs, archived: false });
const page = (task: string, ago: number) =>
  ({ path: `/session/${task.padEnd(16, '0')}`, task, cwd: '/w', opened: NOW - ago, title: `${task} page` });
const FLEET = {
  projs: { dev: P('dev', 'Dev'), kitchen: P('kitchen', 'Kitchen'), learn: P('learn', 'Learn') },
  snap: { surfaces: [page('t-dev', 30), page('t-study', 20)] },
  cadres: { 't-dev': 'dev', 't-study': 'learn' },
  cadres_also: { 't-study': ['dev'] },
  cadre_week_usd: { learn: 0.5 }, running: [], slots: 4,
} as unknown as Fleet;
const screens = ['home', 'review', 'blackboard', 'records', 'agents', 'settings'];
const NAV: Nav = {
  cadre: { id: 'learn', name: 'Learn' },
  screens: screens.map((id) => ({ id, label: id[0].toUpperCase() + id.slice(1), href: id === 'home' ? '/c/learn' : `/c/learn/${id}` })),
  batches: [], pinned: [], cadres: [{ id: 'dev', name: 'Dev' }, { id: 'learn', name: 'Learn' }],
  settings: { project: 'learn', folders: [], members: [], nav: screens },
  agents: [{ id: 'tutor', name: 'Tutor', skill: 'learning', does: 'Runs study sessions.' },
    { id: 'course-builder', name: 'Course builder', skill: 'learning-course', does: 'Builds courses.' },
    { id: 'card-writer', name: 'Card writer', skill: 'learning-cards', does: 'Writes cards.' }],
};
const CARD = (id: string, front: string, source: string) =>
  ({ id, front, source, kind: 'concept', anchor: 't-study', scope: 'personal', due_at: '2026-11-18T00:00:00' });
const DECK: Deck = {
  due: [CARD('c-1', 'Why does a starter need feeding?', 'sourdough')],
  review: [CARD('c-1', 'Why does a starter need feeding?', 'sourdough')],
  proposed: [{ ...CARD('c-2', 'What does a lease buy a consumer?', 'leases'), due_at: '' }],
  discarded: 0, topics: [{ source: 'sourdough', cards: 1, due: 1 }], dirs: ['/w'],
  sessions: [{ session: 't-study', material: 'Sourdough starters', open: true, ready: false, since: NOW,
    purposes: ['skill'], askers: ['kitchen'], page: '/open?file=/s/task-t-study.html' },
  { session: 't-old', material: 'System design', open: false, ready: true, since: NOW - 99,
    purposes: ['course'], askers: ['dev'], page: '/open?file=/s/task-t-old.html' }],
  notice: '',
};

class NoStream {
  addEventListener() {}
  close() {}
  onerror: (() => void) | null = null;
}

function serving(path: string) {
  window.history.pushState(null, '', path);
  vi.stubGlobal('EventSource', NoStream);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const u = new URL(url);
    const body = u.pathname === '/api/fleet' ? FLEET : u.pathname === '/api/nav' ? NAV : u.pathname === '/api/deck' ? DECK
      : u.pathname === '/api/backends' ? { backends: [] } : u.pathname === '/api/newwork' ? { home: '/h', library: [] } : {};
    return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
  }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState(null, '', '/');
});

describe('Learn', () => {
  it('shows a study session in Learn and in the cadre that asked, and nowhere else', () => {
    const home = buildHome(FLEET, serverView(''), NOW, '');
    const shown = (c: string) => home.items.filter((it) => inGroup(home.member[it.key] || [], c)).map((it) => it.key);
    expect(shown('learn')).toEqual(['t-study']);
    expect(shown('dev').sort()).toEqual(['t-dev', 't-study']);
    expect(shown('kitchen')).toEqual([]);
  });

  it('has a Review tab: what is due, the topics, a study box, and proposed cards to keep or drop', async () => {
    serving('/c/learn/review');
    await act(async () => { render(<App />); });
    expect(screen.getAllByText('Why does a starter need feeding?').length).toBeGreaterThan(0);
    expect(screen.getByText('What does a lease buy a consumer?')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'keep' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Study it' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Topics' }).textContent).toContain('sourdough (1, 1 due)');
  });

  it('shows due cards, topics, sessions in progress and courses on its home', async () => {
    serving('/c/learn');
    await act(async () => { render(<App />); });
    const panel = screen.getByRole('region', { name: 'Learning' });
    expect(panel.textContent).toContain('Why does a starter need feeding?');
    expect(panel.textContent).toContain('Sourdough starters');
    expect(panel.textContent).toContain('asked by kitchen');
    expect(panel.textContent).toContain('System design');
    expect(panel.textContent).toContain('Debriefs on offer');
  });

  it('lists its three agents on the Agents tab', async () => {
    serving('/c/learn/agents');
    await act(async () => { render(<App />); });
    for (const name of ['Tutor', 'Course builder', 'Card writer']) expect(screen.getByText(name)).toBeTruthy();
  });
});
