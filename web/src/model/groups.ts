// Groups of work: the home page's sidebar (All, Dev, Everyday, …, Other).
// React-only: the Python page has no sidebar, and with no group defined the
// app draws none, so the two pages stay identical until someone makes one.
//
// A group is a project that another live project names as its group
// (`pipeline.py project set <member> --group <group>`): one kind of object,
// as runnerlib/projects.py says. An item is in a group
//   1. when one of its tags is the group's name or id: the driver's own word,
//      which wins over the project (a recipe started under ~/Projects can be
//      tagged Everyday and leaves Dev);
//   2. else when its project is the group or sits under it, at any depth;
//   3. else in no group, which the sidebar calls Other.
// New work started while a group is chosen is tagged with the group's name
// (the New work dialog prefills Tags), so work begun without a project lands
// in the group the driver was looking at.

import { cmp, hasKey } from './format';
import { DONE, HIDDEN, type Item } from './items';
import type { Project } from './payload';
import { liveProjects } from './projects';

/** The sidebar entry for items in no group. */
export const OTHER = '~other';

/** One sidebar entry: a group's id and name (OTHER for the rest). */
export interface Group {
  id: string;
  name: string;
}

/** The groups: live projects at least one live project belongs to, by name. */
export function fleetGroups(projs: Record<string, Project>): Group[] {
  const live = liveProjects(projs);
  const named = new Set(live.map((p) => p.group).filter((g): g is string => !!g));
  return live.filter((p) => named.has(p.id)).map((p) => ({ id: p.id, name: p.name }))
    .sort((a, b) => cmp(a.name.toLowerCase(), b.name.toLowerCase()));
}

/** The group ids `it` is in, by the rules at the top of this file. */
export function itemGroups(it: Pick<Item, 'project' | 'tags'>, projs: Record<string, Project>, groups: Group[]): string[] {
  const words = new Set(it.tags.map((t) => t.toLowerCase()));
  const tagged = groups.filter((g) => words.has(g.name.toLowerCase()) || words.has(g.id.toLowerCase())).map((g) => g.id);
  if (tagged.length) return tagged;
  const ids = new Set(groups.map((g) => g.id));
  const out: string[] = [];
  const seen = new Set<string>();
  // the item's project may be named by name rather than id; projects.ts resolved it already
  for (let node: string | null | undefined = it.project; node && !seen.has(node); ) {
    seen.add(node);
    if (ids.has(node)) out.push(node);
    node = hasKey(projs, node) ? projs[node].group : null;
  }
  return out;
}

/** Whether `groups` (an item's, from itemGroups) puts it under sidebar entry `group` ('' is All). */
export function inGroup(groups: string[], group: string): boolean {
  return !group || (group === OTHER ? !groups.length : groups.includes(group));
}

/**
 * The sidebar's badge: per entry ('' for All, OTHER, each group id), how
 * many items still on the home page (not done, not hidden) carry a page
 * the driver has not opened.
 */
export function groupBadges(items: Item[], member: Record<string, string[]>, groups: Group[]): Record<string, number> {
  const out: Record<string, number> = { '': 0, [OTHER]: 0 };
  for (const g of groups) out[g.id] = 0;
  for (const it of items) {
    if (!it.unopened || it.state === DONE || it.state === HIDDEN) continue;
    const mine = member[it.key] || [];
    out['']++;
    if (!mine.length) out[OTHER]++;
    for (const g of mine) out[g]++;
  }
  return out;
}

/** The kept group while the sidebar still offers it, else All. */
export function effectiveGroup(kept: string, groups: Group[]): string {
  if (!groups.length || !kept) return '';
  return kept === OTHER || groups.some((g) => g.id === kept) ? kept : '';
}

/** What New work's Tags box starts with when `group` is chosen: the group's name, or nothing. */
export function groupTag(group: string, groups: Group[]): string {
  return groups.find((g) => g.id === group)?.name ?? '';
}
