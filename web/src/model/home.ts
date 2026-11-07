// One machine's fleet payload made into what the home page draws: the items
// in the order the server would draw them (statusd._items_html), what each
// item's rows read (ItemContext), the New work dialog's parent choices, the
// project filter's options, and the stale-code line.

import { collapse } from './conversations';
import { num, relTime, s } from './format';
import { assignStates, bareByTask, fleetItems, itemFacts, order, IDLE, type Item } from './items';
import type { Ending, Fleet, LinkRecord, PageInfo, Pageless, SessionStatus, Surface } from './payload';
import { liveProjects } from './projects';
import { clean, kidsOf, type Tree } from './tree';
import { visible, type ServerView } from './view';

/** What an item's rows read besides the item: shared by every item of one draw. */
export interface ItemContext {
  /** the moment the page was drawn, epoch seconds */
  now: number;
  statuses: Record<string, SessionStatus>;
  spent: Record<string, number>;
  histories: Record<string, number>;
  tree: Tree;
  kids: Record<string, string[]>;
  endings: Record<string, Ending>;
  /** launches with no page, by task */
  bare: Record<string, Pageless>;
  /** live project id -> name */
  names: Record<string, string>;
  /** backends.point_at's data-fold prefix: this backend's URL */
  fold: string;
}

/** Everything the home page draws from one payload. */
export interface Home {
  /** every item, in the order the server draws them: the list, then Idle, then off it */
  items: Item[];
  ctx: ItemContext;
  /** [item key, title]: what New work can be filed under, newest first */
  parents: [string, string][];
  /** the project filter's options: live projects by name, then any other project an item names */
  projects: [string, string][];
  /** every tag on any item, sorted */
  tags: string[];
  /** the collapsed rows and link graph find searches */
  find: { surfaces: Surface[]; records: LinkRecord[]; holders: Record<string, string>; pages: Record<string, PageInfo> };
}

/**
 * statusd.render_fleet(by_item=True) for one payload, as data. `archived`
 * adds keys archived from this browser to those the payload names, so a row
 * leaves the list at once.
 */
export function buildHome(fleet: Fleet, view: ServerView, now: number, origin: string,
  archived: Iterable<string> = []): Home {
  const tree = clean(fleet.tree);
  const kids = kidsOf(tree);
  const [records, holders, pages] = fleet.graph || [[], {}, {}];
  const raw = fleet.snap?.surfaces || [];
  const surfaces = collapse(raw, records, holders, pages);
  const projs = fleet.projs || {};
  const statuses = fleet.statuses || {};
  const pageless = fleet.pageless || [];
  const gone = new Set(archived);

  const returned = fleet.returned || {};
  const spent = fleet.spent || {};
  const endings = fleet.endings || {};
  const bare = bareByTask(pageless);
  const all = fleetItems({ surfaces, tree, projs, statuses, returned, pageless, archived: gone });
  itemFacts(all, { tree, kids, statuses, doing: fleet.doing || {}, returned, spent, now,
    running: fleet.running || [], endings, bare });
  assignStates(all, fleet.items || {}, view.limit);
  const sorted = order(all, view.sort);
  const shown = (it: Item) => visible(it, view.show, view.tag);
  const idles = (it: Item) => view.show === '' && it.state === IDLE;
  const items = [...sorted.filter((it) => shown(it) && !idles(it)),
    ...sorted.filter((it) => shown(it) && idles(it)), ...sorted.filter((it) => !shown(it))];

  const names: Record<string, string> = Object.create(null);
  const projects: [string, string][] = liveProjects(projs).map((p) => [p.id, p.name]);
  for (const [id, name] of projects) names[id] = name;
  for (const it of items) {
    if (it.project && !projects.some(([k]) => k === it.project)) projects.push([it.project, names[it.project] ?? it.project]);
  }
  // statusd.parent_choices: fleet_items without statuses, so newest by when a page opened
  const parents = fleetItems({ surfaces, tree, projs, pageless, archived: gone })
    .filter((it) => !it.key.startsWith('page:')).map((it): [string, string] => [it.key, it.title]);

  return {
    items,
    ctx: { now, statuses, spent, histories: fleet.histories || {}, tree, kids, endings, bare, names, fold: origin },
    parents,
    projects,
    tags: [...new Set(items.flatMap((it) => it.tags))].sort(),
    find: { surfaces: raw, records, holders, pages },
  };
}

/** The limit select's choices: the server's, then (as the page script does) a kept limit it lacks. */
export function limitChoices(serverLimit: number, kept: number): number[] {
  const limits = [...new Set([6, 15, 30, 50, serverLimit])].sort((a, b) => a - b);
  if (kept && !limits.includes(kept)) limits.push(kept);
  return limits;
}

/** statusd.code_line: which process runs older code than the folder holds; null while both are current. */
export function staleCode(fleet: Fleet, now: number): string | null {
  const behind: [string, number, string][] = [];
  const code = fleet.snap?.code || {};
  if (code.running && code.tree && code.running !== code.tree) behind.push(['the daemon', num(code.since), s(code.head)]);
  const own = fleet.fleet_page_code || {};
  if (own.stale) behind.push(['the fleet page', num(own.since), s(own.head)]);
  if (!behind.length) return null;
  const who = behind.map(([name, since, head]) => `${name} (started ${relTime(since, now)}${head ? ` on ${head}` : ''})`)
    .join(' and ');
  const [verb, them] = behind.length === 1 ? ['is', 'it'] : ['are', 'them'];
  return `${who[0].toUpperCase()}${who.slice(1)} ${verb} running code older than this folder; edits wait until you restart ${them}.`;
}
