import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NotificationCenter } from './Bell';
import { ServerProvider } from './server';
import { TopBar } from './TopBar';

// The notifications on every fleet screen read Cadre's /api/notifications
// when the fleet stream says something changed.
const SEEN = 'review-surface:notifications-seen-at';
const CHIMED = 'cadre:notifications-chimed';
const older = { key: 'aaaa', type: 'page.version', label: 'New version', at: '2026-11-17T10:00:00.000Z', title: 'Shop list', url: '/session/aaaa', actionable: true, problem: false };
const newer = { key: 'bbbb', type: 'page.agent_reply', label: 'Agent replied', at: '2026-11-17T10:05:00.000Z', title: 'Pantry plan', url: '/session/bbbb', actionable: true, problem: false };
let rows = [newer, older];
let calls: string[] = [];

/** A fleet stream the test drives: `fire('change')` is the server saying something moved. */
class FakeStream {
  static all: FakeStream[] = [];
  handlers: Record<string, ((e: Event) => void)[]> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { FakeStream.all.push(this); }
  addEventListener(name: string, fn: (e: Event) => void) { (this.handlers[name] ||= []).push(fn); }
  close() { this.closed = true; }
  fire(name: string, data = '{}') { (this.handlers[name] || []).forEach((fn) => fn(new MessageEvent(name, { data }))); }
}

beforeEach(() => {
  rows = [newer, older];
  calls = [];
  FakeStream.all = [];
  localStorage.clear();
  // a browser with Web Locks (HTTPS or localhost); bellStore.test.ts covers the one without
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: async (_: string, fn: () => unknown) => fn() } });
  vi.stubGlobal('EventSource', FakeStream);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    calls.push(url);
    return new Response(JSON.stringify({ notifications: rows }), { headers: { 'Content-Type': 'application/json' } });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

const draw = async (bar = true) => {
  await act(async () => {
    render(<ServerProvider base=""><NotificationCenter>{bar ? <TopBar><h1>Runs</h1></TopBar> : <p>frame</p>}</NotificationCenter></ServerProvider>);
  });
};

describe('the bell', () => {
  it('sits at the right end of the header and counts rows newer than the time seen (the key the old page bell used)', async () => {
    localStorage.setItem(SEEN, '2026-11-17T10:01:00.000Z');
    await draw();
    expect(calls[0]).toMatch(/\/api\/notifications$/);
    const header = document.querySelector('header')!;
    expect(header.lastElementChild?.className).toBe('fbell');
    fireEvent.click(screen.getByRole('button', { name: 'Notifications, 1 new' }));
    const links = screen.getAllByRole('menuitem');
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/view/bbbb', '/view/aaaa']);
    expect(links[0].className).toContain('unread');
    expect(links[1].className).not.toContain('unread');
  });

  it('opening it marks everything seen', async () => {
    localStorage.setItem(SEEN, '2026-11-17T09:00:00.000Z');
    await draw();
    fireEvent.click(screen.getByRole('button', { name: 'Notifications, 2 new' }));
    expect(localStorage.getItem(SEEN)).toBe('2026-11-17T10:05:00.000Z');
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeTruthy();
  });

  it('a first visit takes what is listed as history: no count, nothing to chime for', async () => {
    await draw();
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(CHIMED)!)).toEqual(['bbbb@2026-11-17T10:05:00.000Z', 'aaaa@2026-11-17T10:00:00.000Z']);
  });

  it('re-reads the list when the fleet stream says something changed, on the stream the tab already has', async () => {
    rows = [older];
    localStorage.setItem(SEEN, older.at);
    await draw();
    expect(FakeStream.all).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeTruthy();
    rows = [newer, older];
    vi.useFakeTimers();
    await act(async () => { FakeStream.all[0].fire('change'); await vi.advanceTimersByTimeAsync(300); });
    vi.useRealTimers();
    await act(async () => {});
    expect(screen.getByRole('button', { name: 'Notifications, 1 new' })).toBeTruthy();
    // no tab here can play sound (no click yet), so none claims the chime: one that can will ring
    expect(JSON.parse(localStorage.getItem(CHIMED)!)).toEqual(['aaaa@' + older.at]);
  });

  it('with the sound muted the new row is claimed, so no other tab rings for it either', async () => {
    rows = [older];
    localStorage.setItem(SEEN, older.at);
    localStorage.setItem('review-surface:notifications-sound', 'off');
    await draw();
    rows = [newer, older];
    vi.useFakeTimers();
    await act(async () => { FakeStream.all[0].fire('change'); await vi.advanceTimersByTimeAsync(300); });
    vi.useRealTimers();
    await act(async () => {});
    expect(JSON.parse(localStorage.getItem(CHIMED)!)).toContain('bbbb@' + newer.at);
  });

  it('links a row to the frame on the machine it came from', async () => {
    localStorage.setItem(SEEN + ':http://box-2:8183', '2026-11-17T09:00:00.000Z');
    await act(async () => {
      render(<ServerProvider base="http://box-2:8183"><NotificationCenter><TopBar><h1>Runs</h1></TopBar></NotificationCenter></ServerProvider>);
    });
    fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
    expect(screen.getAllByRole('menuitem')[0].getAttribute('href')).toBe('http://box-2:8183/view/bbbb');
  });

  it('in the frame, the menu leaves out the page shown, but its news still counts toward the chime', async () => {
    localStorage.setItem(SEEN, older.at);
    localStorage.setItem(CHIMED, JSON.stringify(['aaaa@' + older.at]));
    localStorage.setItem('review-surface:notifications-sound', 'off'); // muted: the chime is claimed, so it shows here
    await act(async () => {
      render(<ServerProvider base=""><NotificationCenter exclude="bbbb"><TopBar><h1>Frame</h1></TopBar></NotificationCenter></ServerProvider>);
    });
    expect(calls[0]).toMatch(/\/api\/notifications$/);
    expect(JSON.parse(localStorage.getItem(CHIMED)!)).toContain('bbbb@' + newer.at); // bbbb, the page shown, rang
    fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
    expect(screen.getAllByRole('menuitem').map((a) => a.getAttribute('href'))).toEqual(['/view/aaaa']);
    expect(screen.getByText('On other pages')).toBeTruthy();
  });

  it('an agent at work is listed quietly: no count, no unread mark', async () => {
    localStorage.setItem(SEEN, older.at);
    rows = [{ ...newer, label: 'New version', actionable: false }, older];
    await draw();
    expect(screen.getByRole('button', { name: 'Notifications' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Notifications' }));
    expect(screen.getAllByRole('menuitem')[0].className).toContain('quiet');
  });

  it('a failed run turns the badge to the warning colour, says so, and marks its row', async () => {
    localStorage.setItem(SEEN, older.at);
    rows = [{ ...newer, label: 'Run timed out', problem: true }, older];
    await draw();
    const button = screen.getByRole('button', { name: 'Notifications, 1 new, 1 failed' });
    expect(button.className).toContain('has-problem');
    fireEvent.click(button);
    expect(screen.getAllByRole('menuitem')[0].className).toContain('problem');
    expect(screen.getByLabelText('failed')).toBeTruthy();
    // seen: the bell goes back to its normal colour at once, while the row keeps its warning
    expect(screen.getByRole('button', { name: 'Notifications' }).className).not.toContain('has-problem');
    fireEvent.mouseDown(document.body);
    expect(screen.getByRole('button', { name: 'Notifications' }).className).not.toContain('has-problem');
    expect(document.querySelector('.fbell-alert')).toBeNull(); // menu closed
  });

  it('runs without a header too (the page frame): it still reads and records what to chime for', async () => {
    await draw(false);
    expect(screen.queryByRole('button')).toBeNull();
    expect(calls).toHaveLength(1);
  });

  it('remembers a muted chime', async () => {
    await draw();
    fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Sound on' }));
    expect(localStorage.getItem('review-surface:notifications-sound')).toBe('off');
    expect(screen.getByRole('button', { name: 'Sound off' }).getAttribute('aria-pressed')).toBe('false');
  });
});

