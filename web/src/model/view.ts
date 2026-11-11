// The Work list's view: the one the server draws from the page's query
// (serverView, items.visible), the one the driver keeps in this browser
// (Sel), and what the Python page script's layout() makes of them — states
// by the home limit, the chips' counts, the filters, the sort, and which
// items sit in the list, in Idle, or off it.

import { hasKey, pyFixed, pyInt } from './format';
import { DONE, HIDDEN, HOME_LIMIT, IDLE, SORTS, WORKING, YOUR_TURN, type Item, type State } from './items';

/** Where the kept view lives in localStorage: the same key as the Python page's. */
export const VIEW_KEY = 'cadre.home.v1';
/** The states the home page shows by default. */
export const HOME: State[] = [YOUR_TURN, WORKING, IDLE];
/** The state chips, in order: [state, label]. */
export const CHIPS: [string, string][] = [
  ['your-turn', 'Your turn'], ['working', 'Agent working'], ['idle', 'Idle'],
  ['unopened', 'Unopened'], ['done', 'Done'], ['hidden', 'Hidden'],
];

/** The ?show= values the server knows (items.SHOWS). */
export const SHOWS = ['', 'unopened', 'done', 'hidden', 'all'];

/** items.visible: whether the server's view (?show=, ?tag=) shows this item */
export function visible(it: Item, show = '', tag = ''): boolean {
  if (tag && !it.tags.includes(tag)) return false;
  if (show === 'all') return true;
  if (show === 'done') return it.state === DONE;
  if (show === 'hidden') return it.state === HIDDEN;
  if (show === 'unopened') return it.unopened;
  return it.state === WORKING || it.state === YOUR_TURN || it.state === IDLE;
}

/** The view the server draws from the page's query (?show= ?sort= ?tag= ?limit=). */
export interface ServerView {
  /** one of SHOWS; '' is the home page */
  show: string;
  /** one of SORTS' keys */
  sort: string;
  tag: string;
  /** how many items the home shows before the rest fold into Idle */
  limit: number;
}

/** statusd._serve_home's view: what the server would draw for this query. */
export function serverView(search: string): ServerView {
  const q = new URLSearchParams(search);
  const show = q.get('show') || '';
  const sort = q.get('sort') || '';
  // max(1, int(limit or HOME_LIMIT)), and a limit int() refuses is HOME_LIMIT
  const limit = q.get('limit') ? pyInt(q.get('limit')!) ?? HOME_LIMIT : HOME_LIMIT;
  return {
    show: SHOWS.includes(show) ? show : '',
    sort: hasKey(SORTS, sort) ? sort : 'needs',
    tag: q.get('tag') || '',
    limit: Math.max(1, limit),
  };
}

/** The view, kept in this browser under the same key the Python page uses. */
export interface Sel {
  show: string[];
  unopened: boolean;
  tag: string;
  proj: string;
  sort: string;
  limit: number;
  idleOpen: boolean;
  /** the sidebar's group: '' All, groups.OTHER, or a group id (React only) */
  group: string;
}

/** The kept view, with a link's ?show= ?sort= ?tag= ?limit= winning over it. */
export function loadSel(search: string, storage: Storage | null): Sel {
  const sel: Sel = { show: [...HOME], unopened: false, tag: '', proj: '', sort: '', limit: 0, idleOpen: false, group: '' };
  try {
    Object.assign(sel, JSON.parse(storage?.getItem(VIEW_KEY) || '{}'));
  } catch {
    /* a torn value is the default view */
  }
  const q = new URLSearchParams(search);
  if (q.get('sort')) sel.sort = q.get('sort')!;
  if (q.get('limit')) sel.limit = +q.get('limit')! || 0;
  if (q.has('tag')) sel.tag = q.get('tag')!;
  if (q.has('group')) sel.group = q.get('group')!;
  if (q.has('show')) {
    const v = q.get('show');
    sel.unopened = v === 'unopened';
    sel.show = v === 'done' ? [DONE] : v === 'hidden' ? [HIDDEN]
      : v === 'all' || v === 'unopened' ? [...HOME, DONE, HIDDEN] : [...HOME];
  }
  return sel;
}

/** Keep the view in this browser; private mode keeps it for this page only. */
export function saveSel(sel: Sel, storage: Storage | null) {
  try {
    storage?.setItem(VIEW_KEY, JSON.stringify(sel));
  } catch {
    /* private mode: the view lasts this page */
  }
}

/**
 * The controls as the page script sets them: the kept sort when the server
 * offers it, else the server's; the kept limit, else the server's; the kept
 * project only while the project select has that option.
 */
export function effectiveView(sel: Sel, server: ServerView, projects: [string, string][]):
  { sort: string; limit: number; proj: string } {
  return {
    sort: hasKey(SORTS, sel.sort) ? sel.sort : server.sort,
    limit: sel.limit || server.limit,
    proj: sel.proj && projects.some(([k]) => k === sel.proj) ? sel.proj : '',
  };
}

/** The tag chips: every tag an item carries, sorted, then a kept tag no item carries any more. */
export function tagChips(tags: string[], sel: Sel): string[] {
  return sel.tag && !tags.includes(sel.tag) ? [...tags, sel.tag] : tags;
}

/** layout()'s answer: where each item sits, and the chips' counts. */
export interface Placed {
  /** shown and not idle, in sort order */
  list: Item[];
  /** shown and idle, in sort order, in the Idle fold */
  idle: Item[];
  /** not shown */
  off: Item[];
  count: Record<string, number>;
}

/** What layout() reads besides the items and the kept view. */
export interface LayoutInput {
  /** the find box */
  q: string;
  /** the project filter, '' for all */
  proj: string;
  /** the sidebar's group filter: whether an item is in the chosen group (absent: every item) */
  grouped?: (it: Item) => boolean;
  sort: string;
  limit: number;
  /** an item's visible text, which find matches */
  text: (it: Item) => string;
}

const int = (x: number) => Math.trunc(x);
// the page script sorts on data-spent, which Python wrote as f"{spent:.2f}"
const spent = (it: Item) => parseFloat(pyFixed(it.spent, 2));

/**
 * `items` in the order they are drawn now (the page script re-places what
 * it finds in the DOM). States are re-derived from the limit exactly as the
 * page script does, so a limit change answers without a round trip; the
 * items come back as copies carrying those states.
 */
export function layout(items: Item[], sel: Sel, opts: LayoutInput): Placed {
  // the twin of items.assignStates, as the page script redoes it in the browser
  const stored = (it: Item): State | '' => (it.state === DONE || it.state === HIDDEN ? it.state : '');
  // with a group chosen, the limit and the chips' counts are the group's own
  const scope = opts.grouped ? new Set(items.filter(opts.grouped)) : null;
  const live = items.filter((it) => !stored(it) && (!scope || scope.has(it)));
  const busy = live.filter((it) => it.working).length;
  const quiet = live.filter((it) => !it.working).sort((a, b) => int(b.touched) - int(a.touched));
  const room = Math.max(0, opts.limit - busy);
  const rank = new Map(quiet.map((it, i) => [it, i]));
  const count: Record<string, number> = { unopened: 0 };
  const placed = items.map((it): Item => {
    if (scope && !scope.has(it)) return it;
    const state = stored(it) || (it.working ? WORKING : (rank.get(it) ?? Infinity) < room ? YOUR_TURN : IDLE);
    count[state] = (count[state] || 0) + 1;
    if (it.unopened) count.unopened++;
    // the same object when nothing moved, so an unchanged item is not redrawn
    return state === it.state ? it : { ...it, state };
  });
  const q = opts.q.trim().toLowerCase();
  const shown = placed.filter((it) =>
    (q || sel.show.includes(it.state)) && (!sel.unopened || it.unopened)
    && (!sel.tag || it.tags.includes(sel.tag)) && (!opts.proj || it.project === opts.proj)
    && (!opts.grouped || opts.grouped(it))
    && (!q || opts.text(it).toLowerCase().includes(q)));
  // The page script's sort, which differs from items.order on purpose: it
  // reads the data-* attributes (whole seconds, spent to the cent) and keeps
  // done and hidden below the rest.
  const n = (it: Item, k: 'returned' | 'touched' | 'needs') => int(it[k]);
  const shelf = (it: Item) => (it.state === DONE || it.state === HIDDEN ? 1 : 0);
  const by = opts.sort === 'result' ? (a: Item, b: Item) => n(b, 'returned') - n(a, 'returned')
    : opts.sort === 'untouched' ? (a: Item, b: Item) => n(a, 'touched') - n(b, 'touched')
      : opts.sort === 'spent' ? (a: Item, b: Item) => spent(b) - spent(a)
        : (a: Item, b: Item) => shelf(a) - shelf(b) || n(a, 'needs') - n(b, 'needs')
          || (n(b, 'returned') || n(b, 'touched')) - (n(a, 'returned') || n(a, 'touched'));
  shown.sort(by);
  const on = new Set(shown);
  return {
    list: shown.filter((it) => it.state !== IDLE),
    idle: shown.filter((it) => it.state === IDLE),
    off: placed.filter((it) => !on.has(it)),
    count,
  };
}
