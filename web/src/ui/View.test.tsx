import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ServerProvider } from './server';
import { ViewScreen } from './View';
import { NotificationCenter } from './Bell';

// The frame is a screen of the app: it loads the chosen machine's page, so it
// works whichever machine is chosen. Its bar is the way up and the title
// only: study starts from a thread on the page, so the bar has no study box
// and no "open alone" link.
function answering(view: object) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(view), { headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ViewScreen', () => {
  it("frames this machine's page under a way back and its title, with no study box", async () => {
    answering({ key: 'k1', session: '/session/k1', title: 'Pantry plan', task: 'task-a' });
    await act(async () => { render(<ServerProvider base=""><ViewScreen pageKey="k1" /></ServerProvider>); });
    expect(screen.getByLabelText('Pantry plan').getAttribute('src')).toBe(window.location.origin + '/session/k1');
    // no title on the frame: the app would show it as a tooltip over the page
    expect(document.querySelector('iframe')?.hasAttribute('title')).toBe(false);
    expect(screen.getByText('Pantry plan')).toBeTruthy();
    expect(document.title).toBe('Pantry plan — Cadre');
    expect(screen.getByText('← back').getAttribute('href')).toBe(window.location.origin + '/');
    expect(screen.queryByPlaceholderText(/study a topic/)).toBeNull();
    expect(screen.queryByText(/open alone/)).toBeNull();
  });

  it('goes up to the home of the cadre the page belongs to, named on the link', async () => {
    answering({ key: 'k3', session: '/session/k3', title: 'Curry', task: 'task-c', cadre: 'kitchen', cadre_name: 'Kitchen' });
    await act(async () => { render(<ServerProvider base=""><ViewScreen pageKey="k3" /></ServerProvider>); });
    expect(screen.getByText('← back').getAttribute('href')).toBe(window.location.origin + '/c/kitchen');
    expect(screen.getByText('← back').getAttribute('title')).toBe("back to Kitchen's home");
  });

  it("frames the chosen machine's page", async () => {
    const calls = answering({ key: 'k2', session: '/session/k2', title: '', task: null });
    await act(async () => { render(<ServerProvider base="http://box-2:8183"><ViewScreen pageKey="k2" /></ServerProvider>); });
    expect(calls[0].url).toBe('http://box-2:8183/api/view/k2');
    expect(screen.getByLabelText('Review page').getAttribute('src')).toBe('http://box-2:8183/session/k2');
    expect(screen.queryByText(/open alone/)).toBeNull();
    expect(document.title).toBe('Surface — Cadre');
  });

  // The bell lives in the page's own bar, where it used to be; the frame's bar
  // keeps the way back and the title, slim. The page offers the slot, the frame
  // answers and says what the bell shows, and a click on the page's bell opens
  // Cadre's own menu under it.
  // The messages are Review Surface's (README, "Framing hosts"); its
  // test/chrome-client-queue.test.js pins the same literals from the page's side.
  describe("the bell in the page's bar", () => {
    async function framed(notifications: object[] = []) {
      // the bell's chime lock and stream, as Bell.test.tsx stands them in
      Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: async (_: string, fn: () => unknown) => fn() } });
      vi.stubGlobal('EventSource', class { addEventListener() {} close() {} });
      vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(
        url.includes('/api/notifications') ? { notifications }
          : { key: 'k1', session: '/session/k1', title: 'Pantry plan', task: null }),
        { headers: { 'Content-Type': 'application/json' } })));
      await act(async () => { render(<ServerProvider base=""><NotificationCenter exclude="k1"><ViewScreen pageKey="k1" /></NotificationCenter></ServerProvider>); });
      const iframe = screen.getByLabelText('Pantry plan') as HTMLIFrameElement;
      const page = iframe.contentWindow!;
      const posted: { type: string; [k: string]: unknown }[] = [];
      vi.spyOn(page, 'postMessage').mockImplementation((m: unknown) => { posted.push(m as { type: string }); });
      const say = (data: object, source: Window | null = page) =>
        act(() => { window.dispatchEvent(new MessageEvent('message', { data, source, origin: window.location.origin })); });
      return { iframe, posted, say };
    }
    const frameBell = () => document.querySelector('.viewbar .fbell-button') as HTMLButtonElement | null;

    it('the frame draws no bell of its own while the page carries it, and tells the page what it shows', async () => {
      localStorage.setItem('review-surface:notifications-seen-at', '2026-01-01T00:00:00.000Z');
      const { posted, say } = await framed([
        { key: 'k2', url: '/session/k2', title: 'Other', at: '2026-11-17T10:05:00.000Z', label: 'Run timed out', actionable: true, problem: true },
        { key: 'k3', url: '/session/k3', title: 'Older', at: '2026-01-01T00:00:00.000Z', label: 'Your turn', actionable: true }]);
      expect(frameBell()?.hidden).toBe(true);
      await say({ type: 'review-surface:barHello' });
      expect(posted[0]).toEqual({ type: 'review-surface-host:carryBell' });
      const state = posted.filter((m) => m.type === 'review-surface-host:bellState').at(-1);
      expect(state).toMatchObject({ count: 1, problem: true, open: false, label: 'Notifications, 1 new, 1 failed' });
      expect(frameBell()?.hidden).toBe(true);
    });

    it("a click on the page's bell opens Cadre's menu under it; Escape in the page closes it", async () => {
      const { iframe, posted, say } = await framed();
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1400 });
      iframe.getBoundingClientRect = () => ({ top: 35, left: 0, right: 1400, bottom: 800, width: 1400, height: 765, x: 0, y: 35, toJSON() {} });
      await say({ type: 'review-surface:barHello' });
      await say({ type: 'review-surface:bellClick', rect: { top: 11, right: 1288, bottom: 45, left: 1254 } });
      const menu = screen.getByRole('menu', { name: 'Notifications' });
      expect(menu.style.position).toBe('fixed');
      expect(menu.style.top).toBe(35 + 45 + 6 + 'px');
      expect(menu.style.right).toBe(1400 - 1288 + 'px');
      expect(posted.filter((m) => m.type === 'review-surface-host:bellState').at(-1)).toMatchObject({ open: true });
      await say({ type: 'review-surface:bellClose' });
      expect(screen.queryByRole('menu', { name: 'Notifications' })).toBeNull();
    });

    it("the closing click lands on Cadre's shield, as before", async () => {
      const { say } = await framed();
      await say({ type: 'review-surface:barHello' });
      await say({ type: 'review-surface:bellClick', rect: { top: 11, right: 1288, bottom: 45, left: 1254 } });
      fireEvent.mouseDown(document.querySelector('.fbell-shield')!);
      expect(screen.queryByRole('menu', { name: 'Notifications' })).toBeNull();
    });

    it('a page that loads without offering gets the bell back in the frame bar', async () => {
      const { iframe, say } = await framed();
      await act(async () => { fireEvent.load(iframe); });
      expect(frameBell()?.hidden).toBe(false);
      // a page that offers on its next load takes it back
      await say({ type: 'review-surface:barHello' });
      await act(async () => { fireEvent.load(iframe); });
      expect(frameBell()?.hidden).toBe(true);
    });

    it('messages from any other window, and clicks before the page offered, are ignored', async () => {
      const { posted, say } = await framed();
      await say({ type: 'review-surface:barHello' }, window);
      expect(posted).toEqual([]);
      await say({ type: 'review-surface:bellClick', rect: { top: 1, right: 2, bottom: 3, left: 0 } });
      expect(screen.queryByRole('menu', { name: 'Notifications' })).toBeNull();
    });

    it('a page cannot hang the menu off the window, and a resize closes it', async () => {
      const { iframe, say } = await framed();
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1400 });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
      iframe.getBoundingClientRect = () => ({ top: 35, left: 0, right: 1400, bottom: 800, width: 1400, height: 765, x: 0, y: 35, toJSON() {} });
      await say({ type: 'review-surface:barHello' });
      await say({ type: 'review-surface:bellClick', rect: { top: 0, right: 99999, bottom: -500, left: 0 } });
      const menu = screen.getByRole('menu', { name: 'Notifications' });
      expect(menu.style.right).toBe('12px');
      expect(menu.style.top).toBe('12px');
      await act(async () => { window.dispatchEvent(new Event('resize')); });
      expect(screen.queryByRole('menu', { name: 'Notifications' })).toBeNull();
    });

    it('a page that reloads and offers again is told what the bell shows', async () => {
      const { iframe, posted, say } = await framed();
      await say({ type: 'review-surface:barHello' });
      await act(async () => { fireEvent.load(iframe); });
      posted.length = 0;
      await say({ type: 'review-surface:barHello' });
      expect(posted.map((m) => m.type)).toEqual(['review-surface-host:carryBell', 'review-surface-host:bellState']);
    });
  });
});
