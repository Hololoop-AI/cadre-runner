// One Work list for a cadre's home: the board's active cards and the few
// runs on no card, in one order. A card carries what a run row carries, read
// from its runs: the projects they ran in, when one last came back, what
// they cost, and whether one waits on you. So the same sort and filters
// (sort, project, state) work on both. A run on no card that has been quiet
// for a while is not work in hand: it folds away below the list.

import { cardRuns, cardTally, activeCards, type CardTally } from './kanban';
import { WORKING, YOUR_TURN, IDLE, type Item } from './items';
import type { Fleet, Kanban, KanbanCard } from './payload';

/** How long a run on no card stays in the list after it last moved: a week. */
export const LOOSE_FRESH_S = 7 * 24 * 3600;

/** 0 needs you, 1 an agent is on it, 2 quiet. */
export type Urgency = 0 | 1 | 2;

/** What a card shows in its row besides its tally: read from its runs. */
export interface CardFacts {
  /** the projects its runs ran in, most runs first */
  projects: string[];
  /** when a run on it last came back (0: none yet) */
  returned: number;
  /** what its runs cost */
  spent: number;
}

/**
 * The cards a cadre's home lists: every Ready and Doing card, and a Backlog or
 * Done card only while a run on it is working or came back unopened. Its runs
 * are on a card, so they are not in the list of runs on no card either:
 * leaving the card out would leave that new result nowhere but find.
 */
function homeCards(board: Kanban, fleet: Fleet) {
  const listed = activeCards(board);
  const active = new Set(listed.map((c) => c.id));
  const withRuns = (card: KanbanCard) => {
    const runs = cardRuns(card, fleet);
    return { card, runs, tally: cardTally(runs) };
  };
  return [
    ...listed.map(withRuns),
    ...board.cards.filter((c) => !active.has(c.id)).map(withRuns).filter((x) => x.tally.working || x.tally.fresh),
  ];
}

/** One row of the list: an active card with its tally and facts, or a run on no card. */
export type WorkRow =
  | { kind: 'card'; key: string; card: KanbanCard; tally: CardTally; facts: CardFacts; urgency: Urgency; touched: number }
  | { kind: 'item'; key: string; item: Item; urgency: Urgency; touched: number };

/** The list, what folded below it, and its counts. */
export interface WorkRows {
  rows: WorkRow[];
  /** runs on no card, quiet for longer than LOOSE_FRESH_S: folded below the list */
  older: Item[];
  /** how many rows need you, and how many an agent is on */
  needs: number;
  working: number;
}

/** The list's controls, as the Work list's own Sel keeps them. */
export interface WorkView {
  /** a SORTS key: needs (default), result, untouched, spent */
  sort?: string;
  /** a project id, '' for all */
  proj?: string;
  /** the states shown (the state chips); a card is your-turn, working or idle */
  show?: string[];
  /** only rows with something you have not opened */
  unopened?: boolean;
}

function cardUrgency(t: CardTally): Urgency {
  return t.yourTurn ? 0 : t.working ? 1 : 2;
}

function itemUrgency(it: Item): Urgency {
  return it.state === YOUR_TURN ? 0 : it.state === WORKING || it.working ? 1 : 2;
}

const STATE_OF: Record<Urgency, string> = { 0: YOUR_TURN, 1: WORKING, 2: IDLE };

/** A card's projects, last result and cost, from the home's items for its runs (`items`, by key). */
export function cardFacts(runKeys: string[], items: ReadonlyMap<string, Item>, returned: number[]): CardFacts {
  const count = new Map<string, number>();
  let spent = 0;
  for (const k of runKeys) {
    const it = items.get(k);
    if (!it) continue;
    spent += it.spent || 0;
    if (it.project) count.set(it.project, (count.get(it.project) || 0) + 1);
  }
  const projects = [...count.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([p]) => p);
  return { projects, returned: Math.max(0, ...returned), spent };
}

function rowReturned(r: WorkRow): number {
  return r.kind === 'card' ? r.facts.returned : r.item.returned;
}

function rowSpent(r: WorkRow): number {
  return r.kind === 'card' ? r.facts.spent : r.item.spent;
}

/** items.order's sorts, over both kinds of row. */
function sorter(sort = 'needs'): (a: WorkRow, b: WorkRow) => number {
  if (sort === 'result') return (a, b) => rowReturned(b) - rowReturned(a) || b.touched - a.touched;
  if (sort === 'untouched') return (a, b) => a.touched - b.touched;
  if (sort === 'spent') return (a, b) => rowSpent(b) - rowSpent(a);
  return (a, b) => a.urgency - b.urgency || b.touched - a.touched;
}

/**
 * The cadre's work as one list. `board` is null for a cadre with no board;
 * `loose` are its runs on no card, already filtered by the Work list's own
 * rules (layout); `items` are the home's items by key, which a card's facts
 * are read from; `now` is in seconds.
 */
export function workRows(board: Kanban | null, fleet: Fleet, loose: Item[], now: number,
  items: ReadonlyMap<string, Item> = new Map(), view: WorkView = {}): WorkRows {
  const rows: WorkRow[] = [];
  for (const { card, runs, tally } of board ? homeCards(board, fleet) : []) {
    // a run deep in another item's tree has no item of its own: read its project and cost from the tree
    const known = new Map(runs.map((r): [string, Item] => [r.key, items.get(r.key)
      ?? ({ key: r.key, project: String(fleet.tree?.[r.key]?.project || ''), spent: Number(fleet.spent?.[r.key]) || 0 } as Item)]));
    const facts = cardFacts(runs.map((r) => r.key), known, runs.map((r) => r.returned));
    const urgency = cardUrgency(tally);
    if (view.proj && !facts.projects.includes(view.proj)) continue;
    if (view.show && !view.show.includes(STATE_OF[urgency])) continue;
    if (view.unopened && !tally.fresh) continue;
    const touched = Math.max(Number(card.moved_at) || 0, ...runs.map((r) => r.touched));
    rows.push({ kind: 'card', key: `card:${card.id}`, card, tally, facts, urgency, touched });
  }
  const older: Item[] = [];
  for (const it of loose) {
    const urgency = itemUrgency(it);
    // the time its row shows: its newest result, else when it was last touched
    const at = it.returned || it.touched;
    if (urgency !== 1 && now - at > LOOSE_FRESH_S) older.push(it);
    else rows.push({ kind: 'item', key: it.key, item: it, urgency, touched: at });
  }
  rows.sort(sorter(view.sort));
  return {
    rows, older,
    needs: rows.filter((r) => r.urgency === 0).length,
    working: rows.filter((r) => r.urgency === 1).length,
  };
}
