// Groups of work: the home page's sidebar (Home, Dev, Personal, …, Other).
// React-only: the Python page has no sidebar, and with no group defined the
// app draws none, so the two pages stay identical until someone makes one.
//
// A group is a project that another live project names as its group
// (`pipeline.py project set <member> --group <group>`), or a live project
// with no folder: one kind of object, as runnerlib/projects.py says. A
// folder-less project can hold no work by folder, so its only use is as a
// group that tags fill (Kitchen, before any project is filed under it).
// An item is in a group
//   1. when one of its tags is the group's name or id: the driver's own word,
//      which wins over the project (a recipe started under ~/Projects can be
//      tagged Kitchen and leaves Dev);
//   2. else when its project is the group or sits under it, at any depth;
//   3. else in no group, which the sidebar calls Other.
// New work started while a group is chosen is tagged with the group's name
// (the New work dialog prefills Tags), so work begun without a project lands
// in the group the driver was looking at.

import { own } from './format';
import { DONE, HIDDEN, type Item } from './items';
import type { Project } from './payload';
import { liveProjects } from './projects';

/** The sidebar entry for every item: the whole fleet, which the sidebar calls Home. */
export const HOME_GROUP = '';

/** The sidebar entry for items in no group. */
export const OTHER = '~other';

/** One sidebar entry: a group's id and name (OTHER for the rest). */
export interface Group {
  id: string;
  name: string;
}

/** The groups, by name: live projects at least one live project belongs to, or with no folder. */
export function fleetGroups(projs: Record<string, Project>): Group[] {
  const live = liveProjects(projs);
  const named = new Set(live.map((p) => p.group).filter((g): g is string => !!g));
  return live.filter((p) => named.has(p.id) || (Array.isArray(p.dirs) && !p.dirs.length)).map((p) => ({ id: p.id, name: p.name }));
}

/** A group list's lookups, built once per list (buildHome files every item against the same one). */
interface GroupIndex {
  ids: Set<string>;
  /** a tag, lower-cased -> the group whose name or id it is */
  byWord: Map<string, string>;
}
const INDEXES = new WeakMap<Group[], GroupIndex>();

function indexOf(groups: Group[]): GroupIndex {
  let idx = INDEXES.get(groups);
  if (!idx) {
    const byWord = new Map<string, string>();
    for (const g of groups) {
      byWord.set(g.id.toLowerCase(), g.id);
      byWord.set(g.name.toLowerCase(), g.id);
    }
    idx = { ids: new Set(groups.map((g) => g.id)), byWord };
    INDEXES.set(groups, idx);
  }
  return idx;
}

/**
 * The group ids `it` is in, by the rules at the top of this file: from the
 * groups its tags name (else from its project), up through every group
 * above, so an item tagged with a nested group is in the groups holding it.
 */
export function itemGroups(it: Pick<Item, 'project' | 'tags'>, projs: Record<string, Project>, groups: Group[]): string[] {
  const { ids, byWord } = indexOf(groups);
  const tagged = it.tags.map((t) => byWord.get(t.toLowerCase())).filter((g): g is string => !!g);
  const out: string[] = [];
  const seen = new Set<string>();
  // the item's project may be named by name rather than id; projects.ts resolved it already
  for (const start of tagged.length ? tagged : [it.project]) {
    for (let node: string | null | undefined = start; node && !seen.has(node); ) {
      seen.add(node);
      if (ids.has(node)) out.push(node);
      node = own(projs, node)?.group;
    }
  }
  return out;
}

/** Whether `groups` (an item's, from itemGroups) puts it under sidebar entry `group` (HOME_GROUP: everything). */
export function inGroup(groups: string[], group: string): boolean {
  return !group || (group === OTHER ? !groups.length : groups.includes(group));
}

/**
 * The sidebar's badge: per entry (HOME_GROUP, OTHER, each group id), how
 * many items still on the home page (not done, not hidden) carry a page
 * the driver has not opened.
 */
export function groupBadges(items: Item[], member: Record<string, string[]>, groups: Group[]): Record<string, number> {
  const out: Record<string, number> = { [HOME_GROUP]: 0, [OTHER]: 0 };
  for (const g of groups) out[g.id] = 0;
  for (const it of items) {
    if (!it.unopened || it.state === DONE || it.state === HIDDEN) continue;
    const mine = member[it.key] || [];
    out[HOME_GROUP]++;
    if (!mine.length) out[OTHER]++;
    for (const g of mine) out[g]++;
  }
  return out;
}

/** The kept group while the sidebar still offers it, else HOME_GROUP. */
export function effectiveGroup(kept: string, groups: Group[]): string {
  if (!groups.length || !kept) return HOME_GROUP;
  return kept === OTHER || groups.some((g) => g.id === kept) ? kept : HOME_GROUP;
}

/** What New work's Tags box starts with when `group` is chosen: the group's name, or nothing. */
export function groupTag(group: string, groups: Group[]): string {
  return groups.find((g) => g.id === group)?.name ?? '';
}
