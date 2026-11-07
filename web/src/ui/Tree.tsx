// /tree and /tree/<task>: statusd.render_tree — which session launched
// which, without their review pages.

import { Fragment } from 'react';
import { ancestors, descendants, parentsOf, plural, relTime, s, treeHref, treeRoots, viewHref } from '../model';
import type { TreeNode } from '../model';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

/** What the tree page shows of a session's last result (session_tree.RESULT_CHARS). */
const RESULT_CHARS = 1500;

/** statusd._tree_node: one session, what it is and how it stands. */
export function Node({ task, node, now }: { task: string; node: TreeNode; now: number }) {
  return (
    <div className="tnode">
      <a className="ttitle" href={treeHref(task)}>{s(node.title || task)}</a>
      {node.working && <span className="badge running">working</span>}
      <span className="badge">{s(node.node || 'task') + (node.round ? ` · round ${node.round}` : '')}</span>
      <span className="badge">{s(node.project || '')}</span>
      <span className="badge">{`launched ${relTime(node.since || 0, now)}`}</span>
      {node.page && <a className="hist" href={viewHref(node.page)}>page →</a>}
    </div>
  );
}

/** statusd.tree_list: `task`'s children as nested lists, each with its last result's first line. */
export function Kids({ task, nodes, kids, now, seen = new Set() }: {
  task: string; nodes: Record<string, TreeNode>; kids: Record<string, string[]>; now: number; seen?: Set<string>;
}) {
  const next = new Set([...seen, task]);
  const items = (kids[task] || []).filter((c) => !next.has(c));
  if (!items.length) return null;
  return (
    <ul className="tree">
      {items.map((child) => {
        const node = nodes[child] || {};
        const said = s(node.result).trim().split('\n', 1)[0];
        return (
          <li key={child}>
            <Node task={child} node={node} now={now} />
            {said && <p className="tsaid">{[...said].slice(0, 200).join('')}</p>}
            <Kids task={child} nodes={nodes} kids={kids} now={now} seen={next} />
          </li>
        );
      })}
    </ul>
  );
}

/** The tree screen: every tree, or one session (`task`) and everything it launched. */
export function TreeScreen({ task }: { task: string }) {
  const server = useServer();
  const { data, error } = useOnce(() => server.fetchTree(), server.base);
  const now = Date.now() / 1000;
  if (!data) return <Screen name="Session trees" after=" tree" tab="Session trees — tree"><Pending error={error} /></Screen>;
  const { tree, kids, roots } = treeRoots(data.nodes);
  const meta = <p className="meta"><a href="/">← fleet</a> · <a href="/tree">every tree</a></p>;
  if (!task) {
    return (
      <Screen name="Session trees" after=" tree" tab="Session trees — tree">
        {meta}
        {roots.length ? roots.map((t) => (
          <section key={t} className="card"><Node task={t} node={tree[t]} now={now} /><Kids task={t} nodes={tree} kids={kids} now={now} /></section>
        )) : <section className="card"><p className="empty">No session has launched another yet.</p></section>}
      </Screen>
    );
  }
  const node = tree[task];
  if (!node) {
    return <Screen name="Session trees" after=" tree" tab="Session trees — tree">{meta}<p className="notice">That session launched no others and was launched by none.</p></Screen>;
  }
  const title = s(node.title || task);
  const trail = ancestors(task, parentsOf(tree));
  const result = s(node.result).trim();
  const n = descendants(task, kids);
  return (
    <Screen name={title} after=" tree" tab={`${title} — tree`}>
      {meta}
      <section className="card"><h2>{title}</h2>
        <Node task={task} node={node} now={now} />
        <p className="meta">
          {trail.length ? <>{'launched by '}{trail.map((a, i) => (
            <Fragment key={a}>{i ? ' › ' : ''}<a href={treeHref(a)}>{s(tree[a]?.title || a)}</a></Fragment>
          ))}</> : node.launcher ? `launched by session ${node.launcher}, which this machine does not know as a task`
            : 'launched from outside any session'}
        </p>
        {result ? <p className="tresult">{[...result].slice(0, RESULT_CHARS).join('')}</p> : <p className="empty">No turn has finished yet.</p>}
      </section>
      <section className="card"><h2>{`${n} child session${plural(n)}`}</h2>
        {(kids[task] || []).some((c) => c !== task) ? <Kids task={task} nodes={tree} kids={kids} now={now} />
          : <p className="empty">This session has not launched any others.</p>}
      </section>
    </Screen>
  );
}
