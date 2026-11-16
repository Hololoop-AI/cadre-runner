// The kanban board's rules, copied from runnerlib/kanban.py so the Board
// screen can draw a drag at once, before the server's answer comes back.

import { s as str, taskLabel, treeHref, viewHref } from './format';
import { answered } from './items';
import type { Fleet, Kanban, KanbanCard } from './payload';

/** kanban.DRIVER: who `by` names when the driver added a card */
export const DRIVER = 'driver';

/** kanban.label: "doing" reads as "Doing" */
export function columnLabel(column: string): string {
  return column.charAt(0).toUpperCase() + column.slice(1);
}

/** Each column's cards, in board order (kanban.listing's grouping). */
export function byColumn(board: Kanban): Map<string, KanbanCard[]> {
  const cols = new Map(board.columns.map((c): [string, KanbanCard[]] => [c, []]));
  for (const c of board.cards) cols.get(c.column)?.push(c);
  return cols;
}

/**
 * The cards being worked on: those in the columns between the first (not
 * started) and the last (done), the column furthest along first, each in
 * board order. A done card has left this list.
 */
export function activeCards(board: Kanban): KanbanCard[] {
  const cols = byColumn(board);
  return board.columns.slice(1, -1).reverse().flatMap((c) => cols.get(c) || []);
}

/**
 * kanban._move: the board with `card` in `column`, in front of `before`
 * (a card in that column) or at its end when `before` is ''. A move the
 * server would refuse (an unknown card or column, `before` elsewhere)
 * returns the board as it was.
 */
export function kanbanMove(board: Kanban, card: string, column: string, before: string): Kanban {
  const hit = board.cards.find((c) => c.id === card);
  const anchor = before ? board.cards.find((c) => c.id === before) : undefined;
  if (!hit || !board.columns.includes(column) || before === card || (before && anchor?.column !== column)) return board;
  const cards = board.cards.filter((c) => c !== hit);
  const at = anchor ? cards.indexOf(anchor) : cards.length;
  cards.splice(at, 0, { ...hit, column });
  return { ...board, cards };
}

/** Where a card's fleet item opens: a task's session tree, a lone page's page. */
export function cardItemHref(item: string): string {
  return item.startsWith('page:') ? `/page/${encodeURIComponent(item.slice(5))}` : treeHref(item);
}

/** The keys of every run held by a card on `board` (its runs, and the item it was pulled from). */
export function runsOnCards(board: Kanban | null): Set<string> {
  const out = new Set<string>();
  for (const c of board?.cards || []) {
    if (c.item) out.add(c.item);
    for (const r of c.runs || []) out.add(r);
  }
  return out;
}

/** What a card's work adds up to: how many runs, how many wait on you, how many an agent is on, how many are done. */
export interface CardTally {
  runs: number;
  yourTurn: number;
  working: number;
  done: number;
}

/** One run done for a card, drawn by its own task (it may sit deep inside another item's tree). */
export interface CardRun {
  key: string;
  title: string;
  /** its newest page, else its place in the session tree */
  href: string;
  /** where it stands: an agent on it, a page waiting on you, done, or quiet */
  state: 'working' | 'your-turn' | 'done' | 'idle';
  working: boolean;
  /** when it last came back to you (0: never) */
  returned: number;
  touched: number;
}

/**
 * The runs done for `card` (its runs, then the item it was pulled from), each
 * read from the fleet by its own task: its newest page, whether an agent is
 * on it, whether that page waits on you, and whether it was marked done.
 * Newest touched first. A run this machine knows nothing of is left out.
 */
export function cardRuns(card: KanbanCard, fleet: Fleet): CardRun[] {
  const keys = [...new Set([...(card.runs || []), ...(card.item && !card.item.startsWith('page:') ? [card.item] : [])])];
  const pages = fleet.snap?.surfaces || [];
  const tree = fleet.tree || {};
  const statuses = fleet.statuses || {};
  const out: CardRun[] = [];
  for (const key of keys) {
    const mine = pages.filter((sf) => sf.task === key).sort((x, y) => Number(y.opened || 0) - Number(x.opened || 0));
    const node = tree[key];
    if (!mine.length && !node) continue;
    const head = mine[0];
    const working = !!node?.working || (fleet.running || []).some((c) => c.task === key);
    const done = !!fleet.items?.[key]?.done_at;
    const returned = Number(fleet.returned?.[key] || 0);
    const state = done ? 'done' : working ? 'working' : head && !answered(head, statuses) ? 'your-turn' : 'idle';
    out.push({
      key, state, working, returned,
      title: str(head?.title) || str(node?.title) || taskLabel(key),
      href: head?.path ? viewHref(head.path) : treeHref(key),
      touched: Math.max(returned, Number(head?.opened || 0), Number(node?.since || 0)),
    });
  }
  return out.sort((x, y) => y.touched - x.touched);
}

/** The card's tally from its runs' states (items.ts State). */
export function cardTally(runs: Pick<CardRun, 'state' | 'working'>[]): CardTally {
  return {
    runs: runs.length,
    yourTurn: runs.filter((r) => r.state === 'your-turn').length,
    working: runs.filter((r) => r.working).length,
    done: runs.filter((r) => r.state === 'done').length,
  };
}
