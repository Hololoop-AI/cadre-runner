import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ServerProvider } from './server';
import { ViewScreen } from './View';

// The frame is a screen of the app: it loads the chosen machine's page, so it
// works whichever machine is chosen. Its bar is the way back and the title
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
    expect(screen.getByTitle('surface').getAttribute('src')).toBe(window.location.origin + '/session/k1');
    expect(screen.getByText('Pantry plan')).toBeTruthy();
    expect(document.title).toBe('Pantry plan — Cadre');
    expect(screen.getByText('← back').getAttribute('href')).toBe(window.location.origin + '/');
    expect(screen.queryByPlaceholderText(/study a topic/)).toBeNull();
    expect(screen.queryByText(/open alone/)).toBeNull();
  });

  it("frames the chosen machine's page", async () => {
    const calls = answering({ key: 'k2', session: '/session/k2', title: '', task: null });
    await act(async () => { render(<ServerProvider base="http://box-2:8183"><ViewScreen pageKey="k2" /></ServerProvider>); });
    expect(calls[0].url).toBe('http://box-2:8183/api/view/k2');
    expect(screen.getByTitle('surface').getAttribute('src')).toBe('http://box-2:8183/session/k2');
    expect(screen.queryByText(/open alone/)).toBeNull();
    expect(document.title).toBe('Surface — Cadre');
  });
});
