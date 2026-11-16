import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ServerProvider } from './server';
import { ViewScreen } from './View';

// The frame is a screen of the app: it loads the chosen machine's page, so it
// works whichever machine is chosen, and its study box posts without leaving.
function answering(view: object) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const body = String(url).includes('/api/view/') ? view
      : { notice: 'Study started.', location: '/view/k1?notice=Study+started.' };
    return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ViewScreen', () => {
  it("frames this machine's page under a way back, its title and a study box", async () => {
    const calls = answering({ key: 'k1', session: '/session/k1', title: 'Pantry plan', task: 'task-a' });
    await act(async () => { render(<ServerProvider base=""><ViewScreen pageKey="k1" /></ServerProvider>); });
    expect(screen.getByTitle('surface').getAttribute('src')).toBe(window.location.origin + '/session/k1');
    expect(screen.getByText('Pantry plan')).toBeTruthy();
    expect(document.title).toBe('Pantry plan — Cadre');
    expect(screen.getByText('← fleet').getAttribute('href')).toBe(window.location.origin + '/');
    const topic = screen.getByPlaceholderText(/study a topic/) as HTMLInputElement;
    fireEvent.change(topic, { target: { value: 'FSRS' } });
    await act(async () => { fireEvent.submit(topic.form!); });
    const post = calls.find((c) => c.init?.method === 'POST')!;
    expect(post.url).toBe(window.location.origin + '/study');
    expect(String(post.init?.body)).toBe('topic=FSRS&from=task-a&back=%2Fview%2Fk1');
    expect(screen.getByText('Study started.')).toBeTruthy();
  });

  it("frames the chosen machine's page, and a page that is not a dialogue's has no study box", async () => {
    const calls = answering({ key: 'k2', session: '/session/k2', title: '', task: null });
    await act(async () => { render(<ServerProvider base="http://box-2:8183"><ViewScreen pageKey="k2" /></ServerProvider>); });
    expect(calls[0].url).toBe('http://box-2:8183/api/view/k2');
    expect(screen.getByTitle('surface').getAttribute('src')).toBe('http://box-2:8183/session/k2');
    expect(screen.getByText('open alone ↗').getAttribute('href')).toBe('http://box-2:8183/session/k2');
    expect(screen.queryByPlaceholderText(/study a topic/)).toBeNull();
    expect(document.title).toBe('Surface — Cadre');
  });
});
