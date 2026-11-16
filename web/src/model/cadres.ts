// Cadres own their runs (runnerlib/cadres.py). The server records which
// cadre each run belongs to at launch (fleet.cadres); an item takes its root
// task's cadre from there. An item the server has no record for (a page
// with no task, or an older machine's payload) falls back to the group rule
// in groups.ts, the same rule the server's migration used. No cadre is
// OTHER. Holo, the fleet's home, sees a card per cadre and no runs.

import { DONE, HIDDEN, HOME_LIMIT, YOUR_TURN, type Item } from './items';
import type { Fleet, Project } from './payload';
import { itemGroups, OTHER, type Group } from './groups';

/** The cadre that owns `it`: the server's record for its key, else the group rule's nearest group, else OTHER. */
export function itemCadre(it: Pick<Item, 'key' | 'project' | 'tags'>, owned: Record<string, string>,
  projs: Record<string, Project>, groups: Group[]): string {
  return owned[it.key] || itemGroups(it, projs, groups)[0] || OTHER;
}

/** What Holo's card for one cadre says. */
export interface CadreCard {
  id: string;
  name: string;
  /** items whose turn it is: a page is waiting on the driver (within the home limit, as its own page counts them) */
  waiting: number;
  /** items an agent is working on now */
  running: number;
  /** what its runs spent in the last 7 days */
  weekUsd: number;
}

/**
 * Holo's cards: one per cadre, then Other when anything is in it.
 * `member` is Home.member (item key -> its cadre and the groups above it,
 * none for Other), so a cadre nested in another counts in both; done and
 * hidden items are not counted. Waiting is counted as the cadre's own page
 * does (view.ts layout): its quiet items fill what `limit` leaves after
 * the working ones, and the rest fold into Idle.
 */
export function cadreCards(items: Item[], member: Record<string, string[]>, groups: Group[], fleet: Fleet,
  limit = HOME_LIMIT): CadreCard[] {
  const week = fleet.cadre_week_usd || {};
  const cards: CadreCard[] = [...groups, { id: OTHER, name: 'Other' }]
    .map((g) => ({ id: g.id, name: g.name, waiting: 0, running: 0, weekUsd: Number(week[g.id]) || 0 }));
  const byId = new Map(cards.map((c) => [c.id, c]));
  const quiet = new Map<string, number>();
  for (const it of items) {
    if (it.state === DONE || it.state === HIDDEN) continue;
    const mine = member[it.key] || [];
    for (const id of mine.length ? mine : [OTHER]) {
      const card = byId.get(id);
      if (!card) continue;
      if (it.working) card.running++;
      else quiet.set(id, (quiet.get(id) || 0) + 1);
    }
  }
  for (const c of cards) c.waiting = Math.min(quiet.get(c.id) || 0, Math.max(0, limit - c.running));
  return cards.filter((c) => c.id !== OTHER || c.waiting || c.running || c.weekUsd);
}

/** The items waiting on the driver in one cadre (Needs you), newest first. */
export function needsYou(items: Item[], inCadre: (it: Item) => boolean): Item[] {
  return items.filter((it) => it.state === YOUR_TURN && inCadre(it)).sort((a, b) => b.touched - a.touched);
}
