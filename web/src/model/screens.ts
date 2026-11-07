// What the library, costs, runs and session-tree screens compute besides
// their data, as statusd.py's render_library, render_costs, render_runs and
// render_tree do: the runs screen's views, sorts and links, the trees to
// list, and Python's rounding where those screens show minutes.

import type { TreeNode } from './payload';
import { clean, kidsOf, type Tree } from './tree';

/** recent.VIEWS: the runs screen's views, key -> label, in tab order. */
export const RUN_VIEWS: [string, string][] = [
  ['latest', 'latest return per task'], ['open', 'returned, page still open'],
  ['turns', 'every turn'], ['failed', 'failed turns'],
];

/** recent.SORTS: key -> label; "newest" is the select's blank option. */
export const RUN_SORTS: [string, string][] = [
  ['newest', 'newest return first'], ['oldest', 'oldest return first'],
  ['cost', 'most expensive first'], ['longest', 'longest running first'],
];

/** The runs screen's selection, as statusd._serve_runs reads it off the query. */
export interface RunsSel {
  view: string;
  sort: string;
  project: string;
  node: string;
  q: string;
}

/** The query's view, sort, project, node and q, each trimmed to 200 characters. */
export function runsSel(search: string): RunsSel {
  const q = new URLSearchParams(search);
  const get = (k: string) => (q.get(k) || '').trim().slice(0, 200);
  return { view: get('view'), sort: get('sort'), project: get('project'), node: get('node'), q: get('q') };
}

/** The view render_runs draws: a known view, else "latest". */
export function runsView(sel: RunsSel): string {
  return RUN_VIEWS.some(([k]) => k === sel.view) ? sel.view : 'latest';
}

/** render_runs' href(): /runs with the selection, overridden, empty values left out. */
export function runsHref(sel: RunsSel, over: Partial<RunsSel> = {}): string {
  const all = { ...sel, ...over };
  const q = new URLSearchParams();
  for (const k of ['view', 'sort', 'project', 'node', 'q'] as const) if (all[k]) q.set(k, all[k]);
  const s = q.toString();
  return '/runs' + (s ? '?' + s : '');
}

/** Python's round(): to the nearest integer, an exact half to the even one. */
export function pyRound(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return f % 2 === 0 ? f : f + 1;
}

/** render_tree's index: every session that launched others and was launched by none, newest first. */
export function treeRoots(nodes: Record<string, TreeNode>): { tree: Tree; kids: Record<string, string[]>; roots: string[] } {
  const tree = clean(nodes);
  const kids = kidsOf(tree);
  const roots = Object.keys(tree).filter((t) => !tree[t].parent && kids[t]?.length);
  roots.sort((a, b) => (tree[b].since || 0) - (tree[a].since || 0));
  return { tree, kids, roots };
}

/** Plural "s" for a count, as the Python pages write it. */
export function plural(n: number): string {
  return n !== 1 ? 's' : '';
}

