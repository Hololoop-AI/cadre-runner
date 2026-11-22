// Talking to Python fleet servers (statusd.py): the JSON reads, the fleet
// stream, and the Python page's own form posts. The app reads and posts to
// whichever machine is chosen, directly (the server allows its configured
// peers' pages: statusd.CORS_ORIGINS); `connect` binds the calls to one
// machine. This file is the module's only public entry.
import type {
  Backend, Backends, Blackboard, Notifications, CadreRecords, Deck, CostSummary, Dirs, Fleet, Kanban, Nav, Library, NewWorkInfo, PagePayload, ProjectPayload, Runs, TreePayload,
  ViewInfo,
} from '../model';

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
  /** GET /api/project/<id>: everything the project page draws */
  fetchProject(id: string): Promise<ProjectPayload>;
  /** GET /api/page/<key>: one conversation and the repairs that apply to it */
  fetchPage(key: string): Promise<PagePayload>;
  /** GET /api/view/<key>: what the frame around one surface shows */
  fetchView(key: string): Promise<ViewInfo>;
  /** the URL of a path on this machine, for what the app loads but does not fetch (a framed page) */
  url(path: string): string;
  /** GET /api/kanban: the kanban board, its columns and cards in order */
  fetchKanban(): Promise<Kanban>;
  /** GET /api/nav?cadre=<id>: a cadre's rail, header tabs and side column */
  fetchNav(cadre: string): Promise<Nav>;
  /** GET /api/blackboard: the newest events agents wrote to each other */
  fetchBlackboard(): Promise<Blackboard>;
  /** GET /api/records?cadre=<id>: a cadre's records, each opened as a review page */
  fetchRecords(cadre: string): Promise<CadreRecords>;
  /** GET /api/deck: Learn's deck, its topics and its study sessions */
  fetchDeck(): Promise<Deck>;
  /** GET /api/notifications: Cadre's notification center, review pages that moved */
  fetchNotifications(signal?: AbortSignal): Promise<Notifications>;
  /** POST one of the Python page's forms to this machine (postForm) */
  post(action: string, form: HTMLFormElement | URLSearchParams): Promise<Landing>;
  /** this machine's fleet stream (subscribe) */
  subscribe(onChange: () => void, onLive?: (l: Live) => void): () => void;
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
    fetchProject: (id) => getJson<ProjectPayload>(at('/api/project/' + encodeURIComponent(id))),
    fetchPage: (key) => getJson<PagePayload>(at('/api/page/' + encodeURIComponent(key))),
    fetchView: (key) => getJson<ViewInfo>(at('/api/view/' + encodeURIComponent(key))),
    url: at,
    fetchKanban: () => getJson<Kanban>(at('/api/kanban')),
    fetchNav: (cadre) => getJson<Nav>(at('/api/nav?cadre=' + encodeURIComponent(cadre))),
    fetchBlackboard: () => getJson<Blackboard>(at('/api/blackboard?limit=100')),
    fetchRecords: (cadre) => getJson<CadreRecords>(at('/api/records?cadre=' + encodeURIComponent(cadre))),
    fetchDeck: () => getJson<Deck>(at('/api/deck')),
    fetchNotifications: (signal) => getJson<Notifications>(at('/api/notifications'), signal),
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

/** A form's fields as the urlencoded body the browser would post, with the button that sent it. */
export function formBody(form: HTMLFormElement, submitter?: HTMLElement | null): URLSearchParams {
  const body = new URLSearchParams();
  for (const [k, v] of new FormData(form, submitter ?? undefined)) if (typeof v === 'string') body.append(k, v);
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
 * `source` say how live the stream is, `change` asks for a re-fetch (soon,
 * coalesced), and while the stream is down a slow poll stands in. A stream the
 * browser gave up on is reopened after a pause. A subscriber that joins a
 * stream already open is told where it stands and gets one catch-up change.
 *
 * One connection per browser, not per tab: every Cadre tab on an origin is the
 * same app wanting the same news, and over HTTP/1.1 a browser allows only six
 * connections per host, so a few open tabs used to starve every other request
 * (a page's send among them). Inside a tab every subscriber shares one hub per
 * URL (the screen's lists and the header's bell). Across tabs, where the
 * browser has Web Locks (HTTPS or localhost), the tabs elect one leader per URL
 * with a lock: the leader holds the stream and relays what it hears to the
 * other tabs over a BroadcastChannel. When the leader closes, the lock passes
 * to a tab that was waiting for it, which opens the stream. Without Web Locks
 * each tab holds its own stream.
 */
export function subscribe(onChange: () => void, onLive: (l: Live) => void = () => {}, url = '/fleet/events'): () => void {
  const sub: Sub = { onChange, onLive, soonT: undefined, poll: undefined };
  if (typeof EventSource === 'undefined') {
    onLive('off');
    startPoll(sub);
    return () => stopPoll(sub);
  }
  let hub = HUBS.get(url);
  if (!hub) {
    hub = { url, es: null, subs: new Set(), live: 'connecting', retry: undefined, channel: null, release: null, closed: false, id: Math.random().toString(36).slice(2) };
    HUBS.set(url, hub);
    start(hub);
  } else if (hub.live !== 'connecting') {
    onLive(hub.live);
    if (hub.live === 'off') startPoll(sub);
    soon(sub);
  }
  hub.subs.add(sub);
  const mine = hub;
  let done = false;
  return () => {
    if (done) return;
    done = true;
    mine.subs.delete(sub);
    stopPoll(sub);
    clearTimeout(sub.soonT);
    if (mine.subs.size === 0) {
      stop(mine);
      if (HUBS.get(url) === mine) HUBS.delete(url);
    }
  };
}

/** How long a stream the browser closed for good waits before it is opened again. */
const REOPEN_MS = 5000;

interface Sub {
  onChange: () => void;
  onLive: (l: Live) => void;
  soonT: ReturnType<typeof setTimeout> | undefined;
  poll: ReturnType<typeof setInterval> | undefined;
}
interface Hub {
  url: string;
  es: EventSource | null;
  subs: Set<Sub>;
  live: Live;
  retry: ReturnType<typeof setTimeout> | undefined;
  /** to and from the other tabs; null when this tab holds its own stream */
  channel: BroadcastChannel | null;
  /** set while this tab is the leader: gives the lock back */
  release: (() => void) | null;
  /** every subscriber in this tab has left */
  closed: boolean;
  /** names this tab on the channel, so the leader can answer it alone */
  id: string;
}
const HUBS = new Map<string, Hub>();

/** What travels between tabs: a stream state (`catchUp` after a (re)connect; `to` one tab only) or a change. */
type Relay = { t: 'live'; live: Live; catchUp?: boolean; to?: string } | { t: 'change' } | { t: 'ask'; from: string };

function soon(sub: Sub) {
  clearTimeout(sub.soonT);
  sub.soonT = setTimeout(sub.onChange, 250);
}
function startPoll(sub: Sub) {
  if (!sub.poll) sub.poll = setInterval(sub.onChange, FALLBACK_POLL_MS);
}
function stopPoll(sub: Sub) {
  if (sub.poll) clearInterval(sub.poll);
  sub.poll = undefined;
}

function upstream(e: Event): boolean {
  try {
    return !!JSON.parse((e as MessageEvent<string>).data)?.upstream;
  } catch {
    return false;
  }
}

/** Web Locks and BroadcastChannel both, or neither: a leader needs a way to tell the others. */
function lockManager(): LockManager | undefined {
  if (typeof BroadcastChannel === 'undefined') return undefined;
  return (navigator as Navigator & { locks?: LockManager }).locks;
}

/** Become the URL's leader when the lock comes free, hearing the leader until then. */
function start(hub: Hub) {
  const locks = lockManager();
  if (!locks) return openStream(hub);
  const name = 'cadre:fleet-stream:' + new URL(hub.url, window.location.href).href;
  const channel = new BroadcastChannel(name);
  hub.channel = channel;
  channel.onmessage = (e: MessageEvent<Relay>) => heard(hub, e.data);
  channel.postMessage({ t: 'ask', from: hub.id } satisfies Relay); // where does the leader's stream stand?
  locks.request(name, () => {
    if (hub.closed) return undefined; // every subscriber left while this tab waited
    return new Promise<void>((resolve) => {
      hub.release = resolve;
      openStream(hub);
    });
  }).catch(() => {
    if (!hub.closed && !hub.es) openStream(hub); // no lock to be had: hold a stream of our own
  });
}

function stop(hub: Hub) {
  hub.closed = true;
  hub.es?.close();
  hub.es = null;
  clearTimeout(hub.retry);
  hub.channel?.close();
  hub.channel = null;
  hub.release?.(); // the next waiting tab becomes the leader
  hub.release = null;
}

/** A message from another tab. Only the leader answers a question; only the others take news. */
function heard(hub: Hub, msg: Relay) {
  if (hub.closed) return;
  if (msg.t === 'ask') {
    if (hub.release && hub.live !== 'connecting') hub.channel?.postMessage({ t: 'live', live: hub.live, catchUp: true, to: msg.from } satisfies Relay);
    return;
  }
  if (msg.t === 'live' && msg.to && msg.to !== hub.id) return; // the leader answering another tab
  if (!hub.release) apply(hub, msg);
}

/** What the stream said, in this tab, and (from the leader) in every other tab. */
function emit(hub: Hub, msg: Relay) {
  apply(hub, msg);
  if (hub.release) hub.channel?.postMessage(msg);
}

function apply(hub: Hub, msg: Relay) {
  if (msg.t === 'change') return hub.subs.forEach(soon);
  if (msg.t !== 'live') return;
  hub.live = msg.live;
  hub.subs.forEach((s) => {
    s.onLive(msg.live);
    if (msg.live === 'off') startPoll(s);
    else if (msg.catchUp) {
      stopPoll(s);
      soon(s); // catch up on anything missed while away
    }
  });
}

/** Open the hub's stream and hand its events to every subscriber. */
function openStream(hub: Hub) {
  const es = new EventSource(hub.url);
  hub.es = es;
  es.addEventListener('hello', (e) => emit(hub, { t: 'live', live: upstream(e) ? 'on' : 'half', catchUp: true }));
  es.addEventListener('source', (e) => emit(hub, { t: 'live', live: upstream(e) ? 'on' : 'half' }));
  es.addEventListener('change', () => emit(hub, { t: 'change' }));
  es.onerror = () => {
    emit(hub, { t: 'live', live: 'off' });
    // the browser retries on its own unless it closed the stream for good (a 502, a wrong type)
    if (es.readyState === 2 && hub.subs.size > 0 && HUBS.get(hub.url) === hub) {
      hub.retry = setTimeout(() => { if (HUBS.get(hub.url) === hub && hub.subs.size > 0) openStream(hub); }, REOPEN_MS);
    }
  };
}
