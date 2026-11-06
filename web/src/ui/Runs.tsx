// /runs: statusd.render_runs — every run across every project, in one of a
// few views, sorted and filtered by the query so a view is a link to keep.

import { Fragment, useEffect, type FormEvent } from 'react';
import {
  plural, pyRound, relTime, RUN_SORTS, RUN_VIEWS, runsHref, runsSel, runsView, taskLabel, usd, viewHref,
  type RunRow, type RunsSel,
} from '../model';
import { go } from './router';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

/** statusd._run_row */
function Row({ r, now, perTask }: { r: RunRow; now: number; perTask: boolean }) {
  const turns = perTask && r.turns > 1 ? `${r.turns} turns, ` : '';
  const hover = `${turns}${Math.max(1, pyRound(r.seconds / 60))} min of agent time`;
  const page = r.page || {};
  const badges = (
    <>
      {r.project && <span className="badge">{r.project}</span>}
      {r.node && r.node !== 'task' && <span className="badge">{r.node}</span>}
      {!r.ok && <span className="badge needs">failed</span>}
      {r.open && <span className="chip open">open</span>}
      <span className="badge">{`returned ${relTime(r.ended, now)}`}</span>
      {r.cost !== null && <span className="badge">{usd(r.cost)}</span>}
    </>
  );
  const title = <span className="title">{page.title || taskLabel(r.task)}</span>;
  if (!page.path) return <div className="rowline"><span className="row" title={hover}>{title}{badges}</span></div>;
  return (
    <div className="rowline">
      <a className="row" title={hover} href={viewHref(page.path)}>{title}{badges}<span className="go">open →</span></a>
      <a className="hist" href={`/page/${page.key || ''}`}>manage</a>
    </div>
  );
}

function Options({ name, items, blank, cur }: { name: string; items: [string, string][]; blank: string; cur: string }) {
  return (
    <select name={name} aria-label={name} defaultValue={cur}
      onChange={(e) => submit(e.currentTarget.form!)}>
      <option value="">{blank}</option>
      {items.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  );
}

/** The filter form posts as a GET form would: every field, to /runs. */
function submit(form: HTMLFormElement) {
  const q = new URLSearchParams();
  for (const [k, v] of new FormData(form)) if (typeof v === 'string') q.append(k, v);
  go('/runs?' + q.toString());
}

/** j / k or the arrow keys walk the list; Enter opens the row under focus (statusd._RUNS_JS). */
function useRowKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT')) return;
      const down = e.key === 'j' || e.key === 'ArrowDown';
      const up = e.key === 'k' || e.key === 'ArrowUp';
      const rows = [...document.querySelectorAll<HTMLAnchorElement>('#runs a.row')];
      if ((!down && !up) || !rows.length) return;
      e.preventDefault();
      let i = rows.indexOf(document.activeElement as HTMLAnchorElement);
      i = i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + (down ? 1 : -1)));
      rows[i].focus();
      rows[i].scrollIntoView({ block: 'nearest' });
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
}

/** The runs screen. */
export function RunsScreen() {
  const server = useServer();
  const search = window.location.search;
  const sel: RunsSel = runsSel(search);
  const view = runsView(sel);
  const { data, error } = useOnce(() => server.fetchRuns(search), server.base + search);
  useRowKeys();
  const now = Date.now() / 1000;
  const perTask = view === 'latest' || view === 'open';
  const tabs = RUN_VIEWS.map(([key, label], i) => (
    <Fragment key={key}>{i ? ' · ' : ''}{key === view ? <b>{label}</b> : <a href={runsHref(sel, { view: key })}>{label}</a>}</Fragment>
  ));
  return (
    <Screen name="Runs" tab="Cadre — runs">
      {!data ? <Pending error={error} /> : (
        <>
          <p className="meta"><a href="/">← fleet</a>{' · '}{tabs}</p>
          <form id="runfilter" className="repair" method="get" action="/runs"
            onSubmit={(e: FormEvent<HTMLFormElement>) => { e.preventDefault(); submit(e.currentTarget); }}>
            <input type="hidden" name="view" value={view} />
            <Options name="sort" items={RUN_SORTS.filter(([k]) => k !== 'newest')} blank={RUN_SORTS[0][1]} cur={sel.sort} />
            <Options name="project" items={data.projects.map((p) => [p, p])} blank="every project" cur={sel.project} />
            <Options name="node" items={data.nodes.map((n) => [n, n])} blank="every node" cur={sel.node} />
            <input type="search" name="q" defaultValue={sel.q} placeholder="task or project contains…" aria-label="search" />
            <button type="submit">apply</button>
          </form>
          <p className="meta">{`${data.total} run${plural(data.total)}${data.total > data.limit ? ` · showing the first ${data.limit}` : ''} · j / k or the arrow keys move, Enter opens`}</p>
          <section id="runs" className="card">
            {data.rows.length ? data.rows.map((r, i) => <Row key={i} r={r} now={now} perTask={perTask} />)
              : <p className="empty">Nothing matches this view.</p>}
          </section>
        </>
      )}
    </Screen>
  );
}
