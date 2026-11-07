import { formBody, type Server } from '../api';

// Which screen a path shows. The screens keep the Python pages' paths
// (/library, /costs, /runs, /tree/<task>) so their links stay the Python
// pages' links; while the app is served under /app/, a click on one of those
// links stays inside the app instead of leaving for the Python page.

/** One screen of the app. */
export type Route =
  | { screen: 'home' }
  | { screen: 'library' }
  | { screen: 'costs' }
  | { screen: 'runs' }
  | { screen: 'tree'; task: string }
  | { screen: 'project'; id: string }
  | { screen: 'page'; key: string };

/** Where the app is mounted: '/app' under the Python fleet, '' when it serves / itself. */
export const MOUNT = window.location.pathname === '/app' || window.location.pathname.startsWith('/app/') ? '/app' : '';

/** The screen for a path below the mount; null for a path the app does not draw. */
export function route(path: string): Route | null {
  const p = path.replace(/\/+$/, '') || '/';
  if (p === '/') return { screen: 'home' };
  if (p === '/library' || p === '/costs' || p === '/runs') return { screen: p.slice(1) as 'library' | 'costs' | 'runs' };
  if (p === '/tree') return { screen: 'tree', task: '' };
  const m = /^\/(tree|project|page)\/([^/]+)$/.exec(p);
  if (!m) return null;
  const ref = decodeURIComponent(m[2]);
  return m[1] === 'tree' ? { screen: 'tree', task: ref } : m[1] === 'project' ? { screen: 'project', id: ref } : { screen: 'page', key: ref };
}

/** The route of the page as loaded. */
export function current(): Route {
  return route(window.location.pathname.slice(MOUNT.length) || '/') ?? { screen: 'home' };
}

/** The screens' forms the app posts itself, and the confirmation one of them asks first (render_page's End session). */
const POSTS = /^\/(repair|tasks|items|projects(\/[^/]+\/(tasks|archive))?)$/;
const CONFIRM: Record<string, string> = { end: 'End this review session? The agent stops receiving feedback from it.' };

/**
 * Post a screen's form as the Python page would, then land where the server
 * says: an app screen inside the app (with its notice), anything else as a
 * page of the machine the form went to. Home's own forms are posted by the
 * home screen and never reach here.
 */
export function postScreenForms(server: Server): () => void {
  const onSubmit = (e: SubmitEvent) => {
    const form = e.target as HTMLFormElement;
    const action = form.getAttribute('action') || '';
    if (e.defaultPrevented || form.method !== 'post' || !POSTS.test(action)) return;
    e.preventDefault();
    const body = formBody(form, e.submitter);
    const ask = CONFIRM[body.get('action') || ''];
    if (ask && !window.confirm(ask)) return;
    server.post(action, body)
      .then((to) => (route(to.path) && !server.base ? go(to.location) : window.location.assign(server.base + to.location)))
      .catch((err: unknown) => window.alert(err instanceof Error ? err.message : String(err)));
  };
  document.addEventListener('submit', onSubmit);
  return () => document.removeEventListener('submit', onSubmit);
}

/** Open an app path (e.g. "/runs?view=open") inside the app. */
export function go(path: string) {
  window.location.assign(MOUNT + path);
}

/**
 * While mounted under /app, a plain click on a same-origin link to one of
 * the app's own screens goes to that screen inside the app. Anything else
 * (review pages, /page/<key>, another machine's links) is left alone.
 */
export function keepLinksInApp(): () => void {
  if (!MOUNT) return () => {};
  const onClick = (e: MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null;
    if (!a || (a.target && a.target !== '_self')) return;
    const url = new URL(a.href);
    if (url.origin !== window.location.origin || url.pathname.startsWith(MOUNT + '/') || !route(url.pathname)) return;
    e.preventDefault();
    go(url.pathname + url.search + url.hash);
  };
  document.addEventListener('click', onClick);
  return () => document.removeEventListener('click', onClick);
}
