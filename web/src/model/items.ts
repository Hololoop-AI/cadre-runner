// The home page's items, as statusd.py builds them: fleet_items, item_facts
// and runnerlib/items.py's states, sorts and filters. One item per agent
// thread: a root task of the session tree plus everything started from it.

import type { Ending, ItemRecord, Pageless, Project, RunningCard, SessionStatus, Surface } from './payload';
import { keyOf, statusOf } from './conversations';
import { hasKey, isoEpoch, localMinute, num, own, relTime, s, shortError, taskLabel } from './format';
import { forDir, namesOf } from './projects';
import { ancestors, parentsOf, type Tree } from './tree';

/** An agent is on the item now. */
export const WORKING = 'working';
export const YOUR_TURN = 'your-turn';
export const IDLE = 'idle';
/** Marked done from the item's actions (items.json). */
export const DONE = 'done';
/** Hidden from home from the item's actions (items.json). */
export const HIDDEN = 'hidden';
/** Where an item stands, as the chips and its state badge name it. */
export type State = typeof WORKING | typeof YOUR_TURN | typeof IDLE | typeof DONE | typeof HIDDEN;

/** Each state's badge text. */
export const LABELS: Record<State, string> = {
  working: 'Agent working', 'your-turn': 'Your turn', idle: 'Idle', done: 'Done', hidden: 'Hidden',
};
export const HOME_LIMIT = 15;
/** The sort select's choices: key -> what follows "Sort: ". */
export const SORTS: Record<string, string> = {
  needs: 'needs me first', result: 'newest result', untouched: 'longest untouched', spent: 'most spent',
};
/** A run that never started is called that once this long has passed. */
export const STARTUP_GRACE = 300;

/** One agent thread on the home page: a root task and everything started from it (statusd.fleet_items + item_facts). */
export interface Item {
  key: string;
  head: Surface | null;
  pages: Surface[];
  project: string;
  cwd: string;
  title: string;
  touched: number;
  // item_facts
  agents: number;
  working: boolean;
  bare: string;
  needs: number;
  returned: number;
  spent: number;
  unseen: (boolean | null | undefined)[];
  viewed: number[];
  // assign_states
  tags: string[];
  unopened: boolean;
  state: State;
}

/** surface.unowned */
export function unowned(sf: Surface): boolean {
  return sf.kind === 'external' && !sf.task;
}

/** agent_state_badge: (label, css class) for a session's live agent state */
export function agentStateBadge(st: SessionStatus | null | undefined, owned: boolean | null): [string, string] {
  if (!st || !Object.keys(st).length) return owned === false ? ['no owner', ''] : ['', ''];
  if (st.status === 'ended') return ['ended', ''];
  const pending = Math.trunc(num(st.pending_prompts));
  const presence = st.presence;
  if (pending && presence === 'waiting') {
    if (owned === false) return ['queued — no owner yet, adopting', 'needs'];
    if (owned) return ['queued — agent between turns', 'running'];
    return ['queued — no agent listening', 'needs'];
  }
  if (pending) return ['delivering to agent', 'running'];
  if (presence === 'working') return ['agent working', 'running'];
  if (owned === false) return ['no owner — attach from manage', ''];
  if (st.last_agent_reply_at) return ['agent replied — your turn', 'finished'];
  return ['awaiting you', ''];
}

/** _page_state: agent_state_badge with render_fleet's ownership rule */
export function pageState(sf: Surface, statuses: Record<string, SessionStatus>): [string, string] {
  const owned = sf.kind == null || sf.kind === 'page' || !sf.artifact ? null : !unowned(sf);
  return agentStateBadge(statuses[keyOf(sf)], owned);
}

/** pageless_phrase: what became of a launch with no page; "" while it runs */
export function pagelessPhrase(row: Pageless, now: number): string {
  if (row.running) return '';
  if (row.ran) return 'ran, but wrote no page';
  if (row.failed) return `could not start — ${shortError(row.failed)}`;
  if (row.started) return 'started, then stopped before writing a page';
  if (now - num(row.since) > STARTUP_GRACE) return 'never started';
  return 'queued';
}

/** ending_phrase */
export function endingPhrase(e: Ending, now: number): string {
  const who = e.node === '' || e.node === 'task' || e.node == null ? '' : `${e.node} `;
  const what = e.ok === false ? 'failed' : 'ended without a page';
  return `${who}run ${what} ${relTime(num(e.ended), now)}`.trim();
}

/** _answered: the driver closed this page, or it is titled DECIDED */
export function answered(sf: Surface, statuses: Record<string, SessionStatus>): boolean {
  if (sf.stranded && sf.stranded.length) return false;
  const st = statusOf(sf, statuses);
  return st.status === 'ended' || s(sf.title).includes('DECIDED');
}

function itemRoot(task: string, parents: Record<string, string>): string {
  return (ancestors(task, parents)[0]) ?? task;
}

/** fleet_items' arguments, by their Python names. */
export interface FleetItemsInput {
  surfaces: Surface[];
  tree: Tree;
  projs: Record<string, Project>;
  statuses?: Record<string, SessionStatus>;
  returned?: Record<string, number>;
  pageless?: Pageless[];
  /** keys archived here that the snapshot still carries (statusd._ARCHIVED) */
  archived?: Set<string>;
}

/** fleet_items: one item per agent thread, newest touch first */
export function fleetItems({
  surfaces, tree, projs, statuses = {}, returned = {}, pageless = [], archived = new Set(),
}: FleetItemsInput): Item[] {
  const parents = parentsOf(tree);
  const byName = new Map<string, string>();
  for (const pid of Object.keys(projs)) for (const n of namesOf(projs, pid)) byName.set(n, pid);
  const groups = new Map<string, Surface[]>();
  for (const sf of surfaces) {
    if (!sf.path || archived.has(keyOf(sf))) continue;
    const task = s(sf.task);
    const key = task ? itemRoot(task, parents) : `page:${keyOf(sf)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(sf);
  }
  const out: Item[] = [];
  const touch = (sf: Surface) => {
    const st = statusOf(sf, statuses);
    return Math.max(num(sf.opened), isoEpoch(st.updated_at), num(returned[s(sf.task)]));
  };
  for (const [key, list] of groups) {
    const pages = [...list].sort((a, b) => num(b.opened) - num(a.opened));
    const head = pages.find((sf) => s(sf.task) === key) || pages[0];
    let pid = byName.get(s(head.project));
    if (pid === undefined) {
      const owner = forDir(projs, s(head.cwd));
      pid = owner ? owner.id : s(head.project);
    }
    out.push(blank({
      key, head, pages, project: pid, cwd: s(head.cwd),
      title: s(tree[key]?.title || head.title || head.task || head.kind || 'page'),
      touched: Math.max(...pages.map(touch)),
    }));
  }
  const held = new Set(out.map((it) => it.key));
  for (const row of pageless) {
    const key = itemRoot(s(row.task), parents);
    if (!row.task || held.has(key)) continue;
    held.add(key);
    const top: Partial<Pageless> = key === row.task ? row : {};
    out.push(blank({
      key, head: null, pages: [], project: s(row.project), cwd: s(row.cwd),
      title: s(tree[key]?.title || top.title || taskLabel(key)),
      touched: Math.max(num(row.since), num(returned[row.task])),
    }));
  }
  return out.sort((a, b) => b.touched - a.touched);
}

function blank(it: Pick<Item, 'key' | 'head' | 'pages' | 'project' | 'cwd' | 'title' | 'touched'>): Item {
  return {
    ...it, agents: 0, working: false, bare: '', needs: 1, returned: 0, spent: 0,
    unseen: [], viewed: [], tags: [], unopened: false, state: YOUR_TURN,
  };
}

/** item_facts' arguments besides the items, by their Python names. */
export interface ItemFactsInput {
  tree: Tree;
  kids: Record<string, string[]>;
  statuses: Record<string, SessionStatus>;
  doing: Record<string, string>;
  returned: Record<string, number>;
  spent: Record<string, number>;
  now: number;
  running: RunningCard[];
  endings: Record<string, Ending>;
  /** launches with no page, by task (bareByTask) */
  bare: Record<string, Pageless>;
}

/** Launches with no page, by task: what item_facts and the item rows look runs up in. */
export function bareByTask(pageless: Pageless[]): Record<string, Pageless> {
  const out: Record<string, Pageless> = {};
  for (const r of pageless) if (r.task) out[s(r.task)] = r;
  return out;
}

/**
 * item_facts: what the states, filters and sorts read. It fills the items
 * in place, as the Python does, because fleet_items' items are new to this
 * draw and nothing else holds them yet.
 */
export function itemFacts(items: Item[], {
  tree, kids, statuses, doing, returned, spent, now, running, endings, bare: bareRows,
}: ItemFactsInput): Item[] {
  const under: Record<string, number> = {};
  for (const c of running) {
    if (c && typeof c === 'object' && !c.waiting) under[s(c.item)] = (under[s(c.item)] || 0) + 1;
  }
  for (const it of items) {
    const tasks = new Set(it.pages.map((sf) => s(sf.task)).filter(Boolean));
    if (!it.key.startsWith('page:')) {
      tasks.add(it.key);
      const stack = [...(kids[it.key] || [])];
      while (stack.length) {
        const t = stack.pop()!;
        if (!tasks.has(t)) {
          tasks.add(t);
          stack.push(...(kids[t] || []));
        }
      }
    }
    const states = it.pages.map((sf) => pageState(sf, statuses));
    it.agents = under[it.key] || 0;
    it.working = !!it.agents || states.some(([, cls]) => cls === 'running')
      || [...tasks].some((t) => hasKey(doing, t) || !!own(tree, t)?.working)
      || [...tasks].some((t) => !!bareRows[t]?.running);
    const said = [...tasks].filter((t) => hasKey(endings, t))
      .sort((a, b) => num(endings[b].ended) - num(endings[a].ended))
      .map((t) => endingPhrase(endings[t], now));
    for (const t of tasks) if (hasKey(bareRows, t)) {
      const p = pagelessPhrase(bareRows[t], now);
      if (p) said.push(p);
    }
    it.bare = it.working || !said.length ? '' : said[0];
    it.needs = it.bare || states.some(([label, cls]) => cls === 'needs' || label.includes('your turn')) ? 0 : 1;
    it.returned = Math.max(0, ...[...tasks].map((t) => num(returned[t])));
    it.spent = [...tasks].reduce((a, t) => a + num(spent[t]), 0);
    it.unseen = it.pages.map((sf) => statusOf(sf, statuses).unseen);
    it.viewed = it.pages.map((sf) => isoEpoch(statusOf(sf, statuses).viewed_at));
  }
  return items;
}

/** items.assign_states: hidden and done from the store; past `limit`, idle */
export function assignStates(items: Item[], store: Record<string, ItemRecord>, limit = HOME_LIMIT): Item[] {
  const live: Item[] = [];
  for (const it of items) {
    const rec = store[it.key] || {};
    it.tags = [...(rec.tags || [])];
    it.unopened = it.unseen.some((u) => u === true);
    if (rec.hidden_at) it.state = HIDDEN;
    else if (rec.done_at) it.state = DONE;
    else {
      it.state = it.working ? WORKING : YOUR_TURN;
      live.push(it);
    }
  }
  const quiet = live.filter((it) => !it.working).sort((a, b) => b.touched - a.touched);
  const room = Math.max(0, limit - (live.length - quiet.length));
  for (const it of quiet.slice(room)) it.state = IDLE;
  return items;
}

/** items.order */
export function order(items: Item[], sort = 'needs'): Item[] {
  const by = sort === 'result' ? (a: Item, b: Item) => b.returned - a.returned
    : sort === 'untouched' ? (a: Item, b: Item) => a.touched - b.touched
      : sort === 'spent' ? (a: Item, b: Item) => b.spent - a.spent
        : (a: Item, b: Item) => a.needs - b.needs || (b.returned || b.touched) - (a.returned || a.touched);
  return [...items].sort(by);
}

/** catch_up_text: the ask a Catch me up press sends */
export function catchUpText(it: Item): string {
  const seen = Math.max(0, ...it.viewed.filter(Boolean));
  const since = seen ? localMinute(seen)
    : 'the last time I opened any of its pages (the page server may not know)';
  const pages = it.pages.map((sf) => `- ${sf.title || sf.kind || 'page'}: ${sf.artifact || ''}`).join('\n');
  return `Catch me up on "${it.title}": a short brief, one screen, of what changed `
    + `in this line of work since ${since}. Read its pages and what the agents started `
    + `from it reported; say what is decided, what moved, and what waits on me. `
    + `Change nothing.\n\nIts pages:\n${pages}`;
}
