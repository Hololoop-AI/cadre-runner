// Talking to Python fleet servers (statusd.py): the JSON reads, the fleet
// stream, and the Python page's own form posts. The app reads and posts to
// whichever machine is chosen, directly (the server allows its configured
// peers' pages: statusd.CORS_ORIGINS); `connect` binds the calls to one
// machine. This file is the module's only public entry.
import type { Backend, Backends, CostSummary, Dirs, Fleet, Library, NewWorkInfo, Runs, TreePayload } from '../model';

/** GET a JSON endpoint; a non-2xx answer is an error naming it. */
async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal, cache: 'no-store', headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`GET ${url}: ${res.status}`);
  return (await res.json()) as T;
}

/**
 * GET /api/backends from the server the app was loaded from: the machines to
 * switch between. Absolute, because while another machine is chosen the
 * page's <base> points relative URLs at that machine.
 */
export async function fetchBackends(): Promise<Backend[]> {
  return (await getJson<Backends>(window.location.origin + '/api/backends')).backends || [];
}

/** One machine's API. Every path is root-relative on that machine. */
export interface Server {
  /** the machine's origin, '' for the one the app was loaded from */
  base: string;
  /** GET /api/fleet: everything the home page is drawn from, for this machine */
  fetchFleet(signal?: AbortSignal): Promise<Fleet>;
  /** GET /api/newwork: the home directory and the skill picker's library */
  fetchNewWork(): Promise<NewWorkInfo>;
  /** GET /fs: real directories on this machine completing `q`, as a shell would */
  fetchDirs(q: string): Promise<string[]>;
  /** GET /api/library: everything a launch can pick, and the folders scanned */
  fetchLibrary(): Promise<Library>;
  /** GET /api/costs: where the money went */
  fetchCosts(): Promise<CostSummary>;
  /** GET /api/runs with the runs screen's query (view, sort, project, node, q) */
  fetchRuns(search: string): Promise<Runs>;
  /** GET /api/tree: which session launched which */
  fetchTree(): Promise<TreePayload>;
  /** POST one of the Python page's forms to this machine (postForm) */
  post(action: string, form: HTMLFormElement | URLSearchParams): Promise<Landing>;
  /** this machine's fleet stream (subscribe) */
  subscribe(onChange: () => void, onLive: (l: Live) => void): () => void;
}

/** The API of the machine at `base` ('' = the one the app was loaded from). */
export function connect(base: string): Server {
  // never relative: a chosen machine's <base> would re-aim a relative URL
  const at = (path: string) => (base || window.location.origin) + path;
  return {
    base,
    fetchFleet: (signal) => getJson<Fleet>(at('/api/fleet'), signal),
    fetchNewWork: () => getJson<NewWorkInfo>(at('/api/newwork')),
    fetchDirs: async (q) => (await getJson<Dirs>(at('/fs?q=' + encodeURIComponent(q)))).dirs || [],
    fetchLibrary: () => getJson<Library>(at('/api/library')),
    fetchCosts: () => getJson<CostSummary>(at('/api/costs')),
    fetchRuns: (search) => getJson<Runs>(at('/api/runs' + search)),
    fetchTree: () => getJson<TreePayload>(at('/api/tree')),
    post: (action, form) => postForm(at(action), form),
    subscribe: (onChange, onLive) => subscribe(onChange, onLive, at('/fleet/events')),
  };
}

/** Where a form post landed: the page the Python page would go to, and what happened. */
export interface Landing {
  /** what happened, as the page's ?notice= says it */
  notice: string;
  /** the path the Python page would land on, e.g. "/" or "/page/<key>" */
  path: string;
  /** that path with its ?notice=, to go to when it is not home */
  location: string;
}

/**
 * POST a form the way the browser would, without leaving the app. Asked for
 * JSON, statusd answers {notice, location} instead of its 303, so nothing
 * renders a page only for its notice. An older server still redirects; the
 * notice is then read off the URL fetch followed to. Errors (403
 * cross-origin, 413 too large) throw. It never re-sends.
 */
export async function postForm(action: string, form: HTMLFormElement | URLSearchParams): Promise<Landing> {
  const body = form instanceof URLSearchParams ? form : formBody(form);
  const res = await fetch(action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
    body,
  });
  if (!res.ok) throw new Error(`Not sent (${res.status}${res.statusText ? ' ' + res.statusText : ''}).`);
  if ((res.headers.get('Content-Type') || '').includes('application/json')) {
    const { notice = '', location = '/' } = (await res.json()) as { notice?: string; location?: string };
    return { notice, path: new URL(location, 'http://x').pathname, location };
  }
  const url = res.url ? new URL(res.url) : null;
  return { notice: noticeOf(res.url), path: url?.pathname ?? '/', location: url ? url.pathname + url.search : '/' };
}

/** A form's fields as the urlencoded body the browser would post. */
export function formBody(form: HTMLFormElement): URLSearchParams {
  const body = new URLSearchParams();
  for (const [k, v] of new FormData(form)) if (typeof v === 'string') body.append(k, v);
  return body;
}

/** The `?notice=` a redirect landed on, '' when none. */
export function noticeOf(url: string): string {
  if (!url) return '';
  try {
    return new URL(url, 'http://x').searchParams.get('notice') ?? '';
  } catch {
    return '';
  }
}

/** on = every machine answering · half = one is not · off = stream lost */
export type Live = 'connecting' | 'on' | 'half' | 'off';

/** The page is pushed, not polled: this slow poll runs only while the stream is down. */
export const FALLBACK_POLL_MS = 30000;

/**
 * Subscribe to /fleet/events the way the Python page does: `hello` and
 * `change` re-fetch (debounced 250 ms), `source` reports whether every
 * backend answers, and a lost stream falls back to a 30 s poll.
 */
export function subscribe(onChange: () => void, onLive: (l: Live) => void, url = '/fleet/events'): () => void {
  let soonT: ReturnType<typeof setTimeout> | undefined;
  let poll: ReturnType<typeof setInterval> | undefined;
  const soon = () => {
    clearTimeout(soonT);
    soonT = setTimeout(onChange, 250);
  };
  const startPoll = () => {
    if (!poll) poll = setInterval(onChange, FALLBACK_POLL_MS);
  };
  const stopPoll = () => {
    if (poll) clearInterval(poll);
    poll = undefined;
  };
  const upstream = (e: Event) => {
    try {
      return !!JSON.parse((e as MessageEvent<string>).data)?.upstream;
    } catch {
      return false;
    }
  };
  if (typeof EventSource === 'undefined') {
    onLive('off');
    startPoll();
    return stopPoll;
  }
  const es = new EventSource(url);
  es.addEventListener('hello', (e) => {
    onLive(upstream(e) ? 'on' : 'half');
    stopPoll();
    soon(); // catch up on anything missed while away
  });
  es.addEventListener('source', (e) => onLive(upstream(e) ? 'on' : 'half'));
  es.addEventListener('change', soon);
  es.onerror = () => {
    onLive('off');
    startPoll();
  };
  return () => {
    es.close();
    stopPoll();
    clearTimeout(soonT);
  };
}
