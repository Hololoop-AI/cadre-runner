// Small formatting rules, each the TypeScript twin of a statusd.py helper of
// the same name, so the two pages say things in the same words.

export const VIEW_PATH = '/view/';
export const SESSION_PREFIX = '/session/';
export const TREE_PATH = '/tree';

/** _rel_time */
export function relTime(ts: number, now: number): string {
  if (!ts) return '';
  const delta = Math.max(0, Math.trunc(now - ts));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

/**
 * Python's f"{x:.{d}f}": like toFixed, except that an exact half rounds to
 * even (0.125 -> "0.12"; toFixed says "0.13"). Only an exact half differs.
 */
export function pyFixed(x: number, d: number): string {
  const scaled = x * 10 ** d;
  const floor = Math.floor(scaled);
  if (scaled - floor !== 0.5) return x.toFixed(d);
  return ((floor % 2 === 0 ? floor : floor + 1) / 10 ** d).toFixed(d);
}

/** costs.usd: $0.41, $12.30, $1,176 — cents stop mattering past a hundred dollars */
export function usd(x: number): string {
  const [whole, cents] = pyFixed(x, x >= 100 ? 0 : 2).split('.');
  return '$' + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (cents ? '.' + cents : '');
}

/**
 * Python's `escape(text)[:n]` as the browser then shows it: the cut falls on
 * the escaped text, so an apostrophe (&#x27;) uses six of the n characters.
 * render_page cuts its "replaced by" titles this way.
 */
export function escapedCut(text: string, n: number): string {
  const esc: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;' };
  const cut = [...text.replace(/[&<>"']/g, (c) => esc[c])].slice(0, n).join('');
  const back: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#x27;': "'" };
  return cut.replace(/&(amp|lt|gt|quot|#x27);/g, (m) => back[m]);
}

/** Python's int(text): an integer written in full (spaces, sign and _ allowed), else null. */
export function pyInt(text: string): number | null {
  const m = /^\s*([+-]?\d+(?:_\d+)*)\s*$/.exec(text);
  return m ? parseInt(m[1].replace(/_/g, ''), 10) : null;
}

/**
 * `key in obj` without the prototype, as a Python dict sees it: a project
 * or task named "constructor" is not Object's constructor.
 */
export function hasKey(obj: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/** dict.get(key): the value only when `obj` itself has the key. */
export function own<T>(obj: Record<string, T>, key: string): T | undefined {
  return hasKey(obj, key) ? obj[key] : undefined;
}

/** view_href: a review-surface session opens in the fleet's frame */
export function viewHref(path: unknown): string {
  const p = String(path ?? '');
  return p.startsWith(SESSION_PREFIX) ? VIEW_PATH + p.slice(SESSION_PREFIX.length) : p;
}

/** _tree_href (unescaped; React escapes) */
export function treeHref(task: string): string {
  return `${TREE_PATH}/${encodeURIComponent(task)}`;
}

/** _task_label: `task-fix-the-thing-20260925-124025-badfc9` reads as `fix the thing` */
export function taskLabel(task: string): string {
  let body = String(task).replace(/^task-/, '');
  body = body.replace(/-\d{8}-\d{6}(-[0-9a-f]+)?$/, '');
  return body.replace(/-/g, ' ') || String(task);
}

/** _short_error */
export function shortError(detail: string, limit = 110): string {
  let text = String(detail).split(/\s+/).filter(Boolean).join(' ');
  text = text.replace(/^[A-Za-z_][A-Za-z0-9_.]*(Error|Failed|Exception):\s*/, '');
  return text.length <= limit ? text : text.slice(0, limit - 1).trimEnd() + '…';
}

/** _home_path: ~ for the home directory */
export function homePath(p: string, home: string): string {
  if (!home) return p;
  return p === home || p.startsWith(home + '/') ? '~' + p.slice(home.length) : p;
}

/** _iso_epoch, in seconds */
export function isoEpoch(iso: string | null | undefined): number {
  if (!iso) return 0;
  const t = Date.parse(String(iso));
  return Number.isNaN(t) ? 0 : t / 1000;
}

/** time.strftime("%Y-%m-%d %H:%M", localtime) */
export function localMinute(ts: number): string {
  const d = new Date(ts * 1000);
  const two = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

/** A payload number read defensively: anything not a finite number is 0. */
export function num(x: unknown): number {
  const n = typeof x === 'number' ? x : parseFloat(String(x ?? ''));
  return Number.isFinite(n) ? n : 0;
}

/** A payload string read defensively: null and undefined are ''. */
export function s(x: unknown): string {
  return x == null ? '' : String(x);
}

/** Python's string ordering, for sorted() over names */
export function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}
