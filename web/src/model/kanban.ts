// The kanban board's rules, copied from runnerlib/kanban.py so the Board
// screen can draw a drag at once, before the server's answer comes back.

import { treeHref } from './format';
import type { Kanban, KanbanCard } from './payload';

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
