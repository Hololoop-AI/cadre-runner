// Same-origin, root-relative calls to the Python fleet server.
import type { Fleet } from './types';

export async function fetchFleet(signal?: AbortSignal): Promise<Fleet> {
  const res = await fetch('/api/fleet', { signal, headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`GET /api/fleet: ${res.status}`);
  return (await res.json()) as Fleet;
}

export interface NewTask {
  text: string;
  cwd?: string;
}

/**
 * POST /tasks. The server answers 303 to `/?notice=...`; fetch follows it,
 * so the notice is read off the final URL. Errors before the redirect (403
 * cross-origin, 413 too large) come back as a non-ok response.
 */
export async function submitTask(task: NewTask): Promise<string> {
  const body = new URLSearchParams({ text: task.text });
  if (task.cwd?.trim()) body.set('cwd', task.cwd.trim());
  const res = await fetch('/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
    redirect: 'follow',
  });
  const notice = noticeOf(res.url);
  if (notice) return notice;
  if (!res.ok) throw new Error(`Task not submitted (${res.status} ${res.statusText}).`.replace(' )', ')'));
  return 'Task submitted.';
}

export function noticeOf(url: string): string {
  if (!url) return '';
  try {
    return new URL(url, 'http://x').searchParams.get('notice') ?? '';
  } catch {
    return '';
  }
}

/** live = stream up and backend healthy; degraded = stream up, backend says it is not. */
export type Health = 'connecting' | 'live' | 'degraded' | 'offline';

function upstreamOk(e: Event): boolean {
  try {
    return JSON.parse((e as MessageEvent<string>).data)?.upstream !== false;
  } catch {
    return true;
  }
}

/** Subscribe to /fleet/events; `onChange` fires on connect and on each change. */
export function subscribe(onChange: () => void, onHealth: (h: Health) => void): () => void {
  const es = new EventSource('/fleet/events');
  onHealth('connecting');
  es.addEventListener('hello', (e) => {
    onHealth(upstreamOk(e) ? 'live' : 'degraded');
    onChange(); // catch up on anything missed while disconnected
  });
  es.addEventListener('change', () => onChange());
  es.addEventListener('source', (e) => onHealth(upstreamOk(e) ? 'live' : 'degraded'));
  // the browser retries on its own; the next hello flips this back to live
  es.onerror = () => onHealth('offline');
  return () => es.close();
}
