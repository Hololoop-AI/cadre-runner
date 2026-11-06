// statusd._graph as data: an item drawn as its tree, one row per task,
// source at the top, each task under the one that started it, joined by
// lines the way `git log --graph` joins commits. The component draws it.

import type { Earlier, Ending, Pageless, SessionStatus, Surface } from './payload';
import { statusOf } from './conversations';
import { hasKey, num, s } from './format';
import { answered, pagelessPhrase } from './items';
import type { Tree } from './tree';

export const LANE = 14;
/** Height in px of one graph row; the svg cells meet edge to edge at this height. */
export const ROW = 24;

/** new: unopened · you: opened, unanswered · work: agent on it · bad: failed or no page · done */
export type Dot = 'work' | 'bad' | 'new' | 'you' | 'done';

/** One row of an item's graph: a task (or, opened, one of its earlier rounds), its dot and its lines. */
export interface GraphRow {
  task: string;
  depth: number;
  /** tasks folded into this row (drawn as "+N"); 0 while it is opened */
  hidden: number;
  state: Dot;
  /** path `d` attributes for this row's cell */
  lines: string[];
  /** the page this task wrote, when it wrote one */
  page: Surface | null;
  /**
   * What opening this row shows: its page's earlier rounds plus the tasks a
   * settled branch folds away. 0 when there is nothing behind it.
   */
  history: number;
  /** the row is opened: its history is drawn beneath it */
  open: boolean;
  /** set on a row that is an earlier round of the task above it */
  round: Earlier | null;
}

/** An item drawn as the tree that made it. */
export interface Graph {
  width: number;
  rows: GraphRow[];
  /** pages whose task is in no tree; drawn as plain rows above the graph */
  loose: Surface[];
}

/** The x of a lane's line and dot, by depth. */
export const x = (d: number) => 7 + d * LANE;

/**
 * statusd._graph as data: rows in preorder, settled branches folded to one
 * row. `open` names rows the driver opened to see their history.
 */
export function buildItemGraph(
  root: string, pages: Surface[], tree: Tree, kids: Record<string, string[]>,
  statuses: Record<string, SessionStatus>, now: number, endings: Record<string, Ending>,
  bare: Record<string, Pageless>, open: ReadonlySet<string> = new Set(),
): Graph {
  const sfOf = new Map<string, Surface>();
  for (const sf of pages) {
    const t = s(sf.task);
    if (num(sf.opened) >= num(sfOf.get(t)?.opened)) sfOf.set(t, sf);
  }
  const state = (t: string): Dot => {
    if (tree[t]?.working) return 'work';
    if (hasKey(endings, t) || (hasKey(bare, t) && pagelessPhrase(bare[t], now))) return 'bad';
    const sf = sfOf.get(t);
    if (sf && statusOf(sf, statuses).unseen === true) return 'new';
    if (sf && !answered(sf, statuses)) return 'you';
    return 'done';
  };

  // Opening a row (beyond the Python page, which has no such control) hangs
  // its page's earlier rounds under it, newest first, and keeps a settled
  // branch under it unfolded. A round is a row of its own, keyed round:<key>.
  const roundOf = new Map<string, Earlier>();
  const folds = new Map<string, number>();
  const seen = new Set<string>();
  type Raw = [string, number, string[], number];
  const walk = (t: string, depth: number): Raw[] => {
    seen.add(t);
    const kin = (kids[t] || []).filter((c) => !seen.has(c));
    const me: Raw = [t, depth, [], 0];
    let rows: Raw[] = [me];
    if (open.has(t)) {
      for (const e of sfOf.get(t)?.earlier || []) {
        const k = `round:${e.key}`;
        roundOf.set(k, e);
        me[2].push(k);
        rows.push([k, depth + 1, [], 0]);
      }
    }
    for (const c of kin) {
      let sub = walk(c, depth + 1);
      const tasks = sub.filter((r) => !roundOf.has(r[0]));
      if (tasks.length > 1 && tasks.every((r) => state(r[0]) === 'done')) {
        folds.set(c, tasks.slice(1).reduce((a, r) => a + 1 + r[3], 0));
        if (!open.has(c)) {
          sub[0][3] = folds.get(c)!;
          sub[0][2] = [];
          sub = sub.slice(0, 1);
        }
      }
      me[2].push(c);
      rows = rows.concat(sub);
    }
    return rows;
  };

  const rows = walk(root, 0);
  const at = new Map(rows.map((r, i) => [r[0], i]));
  const width = (Math.max(...rows.map((r) => r[1])) + 1) * LANE + 4;
  const cy = ROW / 2;
  const h = ROW;
  const lines: string[][] = rows.map(() => []);
  rows.forEach(([, d, drawn], i) => {
    if (!drawn.length) return;
    const xp = x(d);
    const last = Math.max(...drawn.map((c) => at.get(c)!));
    lines[i].push(`M${xp},${cy} V${h}`);
    for (let j = i + 1; j <= last; j++) {
      if (drawn.includes(rows[j][0])) {
        const xc = x(rows[j][1]);
        lines[j].push(`M${xp},0 V${cy - 5} Q${xp},${cy} ${xp + 5},${cy} H${xc - 4}`);
        if (j < last) lines[j].push(`M${xp},${cy} V${h}`);
      } else {
        lines[j].push(`M${xp},0 V${h}`);
      }
    }
  });

  return {
    width,
    rows: rows.map(([t, d, , hidden], i) => {
      const round = roundOf.get(t) ?? null;
      const page = round ? null : sfOf.get(t) ?? null;
      return {
        task: t, depth: d, hidden, state: round ? 'done' : state(t), lines: lines[i], page, round,
        history: round ? 0 : (page?.earlier || []).length + (folds.get(t) ?? 0),
        open: !round && open.has(t),
      };
    }),
    loose: [...sfOf.entries()].filter(([t]) => !seen.has(t)).map(([, sf]) => sf),
  };
}
