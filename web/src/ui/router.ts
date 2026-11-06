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
  | { screen: 'tree'; task: string };

/** Where the app is mounted: '/app' under the Python fleet, '' when it serves / itself. */
export const MOUNT = window.location.pathname === '/app' || window.location.pathname.startsWith('/app/') ? '/app' : '';

/** The screen for a path below the mount; null for a path the app does not draw. */
export function route(path: string): Route | null {
  const p = path.replace(/\/+$/, '') || '/';
  if (p === '/') return { screen: 'home' };
  if (p === '/library' || p === '/costs' || p === '/runs') return { screen: p.slice(1) as 'library' | 'costs' | 'runs' };
  if (p === '/tree') return { screen: 'tree', task: '' };
  const m = /^\/tree\/([^/]+)$/.exec(p);
  return m ? { screen: 'tree', task: decodeURIComponent(m[1]) } : null;
}

/** The route of the page as loaded. */
export function current(): Route {
  return route(window.location.pathname.slice(MOUNT.length) || '/') ?? { screen: 'home' };
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
