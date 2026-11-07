// runnerlib/session_tree.py, the parts a renderer holding only the JSON
// works from: which session launched which.

import { cmp } from './format';
import type { TreeNode } from './payload';

/** The launch tree, cleaned: task -> its node, parent '' at a root, since 0 when unknown. */
export type Tree = Record<string, TreeNode & { parent: string; since: number }>;

/** clean: drop non-dict nodes, blank a non-string parent and a non-number since */
export function clean(nodes: unknown): Tree {
  const out: Tree = {};
  if (!nodes || typeof nodes !== 'object' || Array.isArray(nodes)) return out;
  for (const [t, n] of Object.entries(nodes as Record<string, unknown>)) {
    if (!n || typeof n !== 'object' || Array.isArray(n)) continue;
    const node = n as TreeNode;
    out[String(t)] = {
      ...node,
      parent: typeof node.parent === 'string' ? node.parent : '',
      since: typeof node.since === 'number' ? node.since : 0,
    };
  }
  return out;
}

/** {child: parent} from the tree, for the roots-first walk up from a session. */
export function parentsOf(nodes: Tree): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [t, n] of Object.entries(nodes)) if (n.parent) out[t] = n.parent;
  return out;
}

/** kids_of: {parent: [children, oldest launch first]} */
export function kidsOf(nodes: Tree): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const [child, parent] of Object.entries(parentsOf(nodes))) (out[parent] ||= []).push(child);
  const since = (t: string) => nodes[t]?.since || 0;
  for (const kids of Object.values(out)) {
    kids.sort((a, b) => since(a) - since(b) || cmp(a, b));
  }
  return out;
}

/** ancestors: root first, ending at task's parent */
export function ancestors(task: string, parents: Record<string, string>): string[] {
  const out: string[] = [];
  const seen = new Set([task]);
  let node = parents[task];
  while (node && !seen.has(node)) {
    seen.add(node);
    out.push(node);
    node = parents[node];
  }
  return out.reverse();
}

/** descendants: how many sessions sit anywhere under task */
export function descendants(task: string, kids: Record<string, string[]>): number {
  let n = 0;
  const stack = [...(kids[task] || [])];
  const seen = new Set([task]);
  while (stack.length) {
    const t = stack.pop()!;
    if (seen.has(t)) continue;
    seen.add(t);
    n += 1;
    stack.push(...(kids[t] || []));
  }
  return n;
}
