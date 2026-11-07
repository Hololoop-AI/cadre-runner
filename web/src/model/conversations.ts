// runnerlib/conversations.py in TypeScript: fold a dialogue's rounds into one
// row (collapse) and find any page ever opened (find). Same rules, same order.

import type { LinkRecord, PageInfo, SessionStatus, Surface } from './payload';
import { hasKey, num, s } from './format';

export const ROOT_HOLDERS = new Set(['root', 'home']);
const SESSION_KEY = /^[0-9a-f]{16}$/;

/** A surface's session key: the last part of its /session/<key> path. */
export function keyOf(sf: Surface): string {
  const p = s(sf.path);
  return p.slice(p.lastIndexOf('/') + 1);
}

/** A page's live state from the page server, {} when it says nothing about the page. */
export function statusOf(sf: Surface, statuses: Record<string, SessionStatus>): SessionStatus {
  return statuses[keyOf(sf)] || {};
}

/** build_graph: (parent, replaced_by). Later records win. */
export function buildGraph(records: LinkRecord[]): [Map<string, string>, Map<string, string>] {
  const parent = new Map<string, string>();
  const replacedBy = new Map<string, string>();
  for (const r of records) {
    if (r.type === 'child-of') parent.set(r.from, r.to);
    else if (r.type === 'supersedes') replacedBy.set(r.to, r.from);
  }
  return [parent, replacedBy];
}

/** cwd_project */
export function cwdProject(cwd: string, surfaces: Surface[]): string {
  if (!cwd) return '';
  const named = new Map<string, number>();
  for (const sf of surfaces) {
    if (sf.project && sf.cwd === cwd) named.set(s(sf.project), (named.get(s(sf.project)) ?? 0) + 1);
  }
  if (named.size) {
    let best = '';
    let n = -1;
    for (const [k, v] of named) if (v > n) [best, n] = [k, v];
    return best;
  }
  const parts = cwd.split('/').filter((p) => p !== '' && !p.startsWith('.'));
  return parts.length ? parts[parts.length - 1] : '';
}

function when(key: string, byKey: Map<string, Surface>, pages: Record<string, PageInfo>): number {
  const sf = byKey.get(key);
  if (sf !== undefined && sf.opened) return num(sf.opened);
  return num(pages[key]?.updated);
}

/** collapse: snapshot surface rows in, one row per conversation out. */
export function collapse(
  surfaces: Surface[],
  records: LinkRecord[] = [],
  holders: Record<string, string> = {},
  pages: Record<string, PageInfo> = {},
): Surface[] {
  const [parent, replacedBy] = buildGraph(records);
  const byKey = new Map<string, Surface>();
  for (const sf of surfaces) if (keyOf(sf)) byKey.set(keyOf(sf), sf);

  const root = new Map<string, string>();
  const find = (x: string): string => {
    if (!root.has(x)) root.set(x, x);
    while (root.get(x) !== x) {
      root.set(x, root.get(root.get(x)!)!);
      x = root.get(x)!;
    }
    return x;
  };
  const union = (a: string, b: string) => root.set(find(a), find(b));

  for (const [oldK, newK] of replacedBy) union(oldK, newK);
  const derived = records
    .filter((r) => r.type === 'derived-from' && !byKey.has(r.to))
    .map((r) => [r.from, r.to] as const);
  for (const [newK, oldK] of derived) union(newK, oldK);
  const byTask = new Map<string, string[]>();
  for (const [k, sf] of byKey) {
    if (sf.task) {
      const t = String(sf.task);
      if (!byTask.has(t)) byTask.set(t, []);
      byTask.get(t)!.push(k);
    }
  }
  for (const ks of byTask.values()) for (const k of ks.slice(1)) union(ks[0], k);

  const groups = new Map<string, Set<string>>();
  for (const k of byKey.keys()) {
    const r = find(k);
    if (!groups.has(r)) groups.set(r, new Set());
    groups.get(r)!.add(k);
  }
  const chain = new Set<string>([...replacedBy.keys(), ...replacedBy.values(), ...derived.flat()]);
  for (const k of chain) {
    const r = find(k);
    if (groups.has(r) && (byKey.has(k) || hasKey(pages, k))) groups.get(r)!.add(k);
  }

  const projectOf = (target: string): string => {
    if (ROOT_HOLDERS.has(target)) return '';
    if (hasKey(holders, target)) return target;
    const sf = byKey.get(target);
    if (sf) return s(sf.project || sf.title || '');
    if (hasKey(pages, target)) return s(pages[target].title || '');
    return SESSION_KEY.test(target) ? '' : target;
  };

  const out: Surface[] = surfaces.filter((sf) => !keyOf(sf));
  for (const members of groups.values()) {
    const order = [...members].sort((a, b) => when(b, byKey, pages) - when(a, byKey, pages));
    const live = order.filter((k) => !replacedBy.has(k));
    const headKey = (live.length ? live : order)[0];
    let head: Surface;
    if (byKey.has(headKey)) head = { ...byKey.get(headKey)! };
    else {
      const pg = pages[headKey];
      head = { kind: 'page', path: `/session/${headKey}`, stranded: [], artifact: pg.file || '', ended: !!pg.ended };
    }
    const memberRows = order.filter((k) => byKey.has(k)).map((k) => byKey.get(k)!);
    head.stranded = memberRows.flatMap((sf) => sf.stranded || []);
    head.opened = Math.max(...[...members].map((k) => when(k, byKey, pages)));
    let linked = '';
    for (const k of [headKey, ...order]) {
      if (parent.has(k) && projectOf(parent.get(k)!)) {
        linked = projectOf(parent.get(k)!);
        break;
      }
    }
    head.project =
      linked ||
      memberRows.find((sf) => sf.project)?.project ||
      cwdProject(s(memberRows.find((sf) => sf.cwd)?.cwd || ''), surfaces) ||
      null;
    if (!head.title) {
      head.title = memberRows.find((sf) => sf.title)?.title || pages[headKey]?.title || head.task || '';
    }
    if (!head.task) head.task = memberRows.find((sf) => sf.task)?.task ?? null;
    head.page_title = s(pages[headKey]?.title || head.title);
    head.earlier = order
      .filter((k) => k !== headKey)
      .map((k) => ({
        key: k,
        path: `/session/${k}`,
        title: s(pages[k]?.title || byKey.get(k)?.title || k),
        opened: when(k, byKey, pages),
      }));
    out.push(head);
  }
  return out.sort((a, b) => num(b.opened) - num(a.opened));
}

/** One page find matched, with where it lives and whether it is the live round. */
export interface Hit {
  key: string;
  path: string;
  title: string;
  file: string;
  project: string;
  state: string;
  updated: number;
  title_hit: boolean;
}

/** find: every page ever opened that matches all the words. */
export function findPages(
  query: string,
  surfaces: Surface[],
  records: LinkRecord[],
  holders: Record<string, string>,
  pages: Record<string, PageInfo>,
  limit = 30,
): Hit[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const [, replacedBy] = buildGraph(records);
  const rows = collapse(surfaces, records, holders, pages);
  const headOf = new Map<string, Surface>();
  for (const row of rows) {
    headOf.set(keyOf(row), row);
    for (const e of row.earlier || []) headOf.set(e.key, row);
  }
  const byKey = new Map<string, Surface>();
  for (const sf of surfaces) if (keyOf(sf)) byKey.set(keyOf(sf), sf);
  const hits: Hit[] = [];
  for (const key of new Set([...Object.keys(pages), ...byKey.keys()])) {
    const sf: Surface = byKey.get(key) || {};
    const pg: PageInfo = pages[key] || {};
    const head: Surface = headOf.get(key) || {};
    const title = s(sf.title || pg.title || key);
    const hayTitle = [title, s(pg.title || '')].join(' ').toLowerCase();
    const hay = [hayTitle, s(pg.file || sf.artifact || ''), s(head.project || sf.project || ''),
      s(sf.task || head.task || ''), key].join(' ').toLowerCase();
    if (!words.every((w) => hay.includes(w))) continue;
    const state = replacedBy.has(key) ? 'replaced'
      : byKey.has(key) && !sf.ended ? 'open'
        : pg.ended ? 'ended' : 'page';
    hits.push({
      key, path: `/session/${key}`, title, file: s(pg.file || sf.artifact || ''),
      project: s(head.project || sf.project || ''), state, updated: when(key, byKey, pages),
      title_hit: words.every((w) => hayTitle.includes(w)),
    });
  }
  hits.sort((a, b) => Number(!a.title_hit) - Number(!b.title_hit) || b.updated - a.updated);
  return hits.slice(0, limit);
}
