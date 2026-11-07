// /project/<id>: statusd.render_project — one project's own page: the
// launcher, what runs in it (render_fleet's per-project section), what
// finished, its context store, and what a task launched here is told.

import { Fragment } from 'react';
import {
  clean, collapse, kidsOf, descendants, keyOf, liveBadge, plural, projectSection, relTime, runLibrary, s, staleCode,
  startingWhat, taskLabel, touchedAt, treeHref, usd, viewHref,
  type ProjectPayload, type Surface, type Tree,
} from '../model';
import { Menu } from './Item';
import { RunPicker } from './NewWork';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';
import { Kids } from './Tree';

/** What one row of the running section reads besides the row. */
interface RowCtx {
  d: ProjectPayload;
  now: number;
  tree: Tree;
  kids: Record<string, string[]>;
}

/** statusd.stranded_badge: a strand outranks everything, with a link per dead letter, newest first. */
function Stranded({ names }: { names: string[] }) {
  if (!names.length) return null;
  return (
    <>
      <span className="badge needs">{`stranded — feedback reached no one${names.length > 1 ? ` (${names.length})` : ''}`}</span>
      {[...names].reverse().map((n, i) => <a key={i} className="hist" href={`/stranded/${n}`}>{`dead letter ${names.length - i}`}</a>)}
    </>
  );
}

/** render_fleet's _live: the strand, the agent's state, when it returned and when anything touched it. */
function Live({ sf, c }: { sf: Surface; c: RowCtx }) {
  const strand = <Stranded names={sf.stranded || []} />;
  if (s(sf.title).includes('DECIDED')) {
    return sf.stranded && sf.stranded.length ? strand : <span className="badge finished">decided — nothing needs you</span>;
  }
  const [label, cls] = liveBadge(sf, c.d.statuses, c.d.doing);
  const back = c.d.returned[s(sf.task)];
  const upd = touchedAt(sf, c.d.statuses);
  return (
    <>
      {strand}
      {label && <span className={`badge ${cls}`}>{label}</span>}
      {back ? <span className="badge" title="when the agent's newest turn on this task ended">{`returned ${relTime(back, c.now)}`}</span> : null}
      {upd ? <span className="badge" title="when anything last touched this page — you opening it, your feedback, or the agent">{`touched ${relTime(upd, c.now)}`}</span> : null}
    </>
  );
}

/** One open conversation in the section, as render_fleet's _group_rows draws it. */
function Row({ sf, c }: { sf: Surface; c: RowCtx }) {
  const title = s(sf.title || sf.kind || '');
  const role = sf.role ? <span className="badge">{sf.role}</span> : null;
  const when = <span className="badge">{relTime(sf.opened || 0, c.now)}</span>;
  if (sf.path) {
    const task = s(sf.task);
    const returned = c.d.returned[task];
    const ago = relTime(sf.opened || 0, c.now);
    const spent = c.d.spent[task];
    const n = Math.trunc(c.d.histories[s(sf.artifact)] || 0);
    const parent = c.tree[task]?.parent;
    const tree = c.tree;
    const k = c.kids[task]?.length ? descendants(task, c.kids) : 0;
    return (
      <>
        <div className="rowline">
          <a className="row" title={returned && ago ? `page opened ${ago}` : undefined} href={viewHref(sf.path)}>
            <span className="title">{title}</span><Live sf={sf} c={c} />{role}
            {returned ? null : <span className="badge">{ago}</span>}
            {spent ? <span className="badge cost" title="what this task's finished turns cost">{usd(spent)}</span> : null}
            <span className="go">open →</span>
          </a>
          {parent ? <a className="hist" href={treeHref(parent)} title={`launched by ${s(c.tree[parent]?.title || parent)}`}>↳ parent</a> : null}
          {n ? <a className="hist" href={`/history/${keyOf(sf)}`}>{`history (${n})`}</a> : null}
          <Menu sf={sf} />
        </div>
        {k ? (
          <details className="kids"><summary>{`${k} child session${plural(k)}`}</summary>
            <Kids task={task} nodes={tree} kids={c.kids} now={c.now} />
            <a className="hist" href={treeHref(task)}>the whole tree →</a>
          </details>
        ) : null}
      </>
    );
  }
  if (sf.cwd) {
    return (
      <div className="story dispatch" role="button" tabIndex={0} aria-pressed="false" data-cwd={s(sf.cwd)} data-name={title}>
        <div className="line"><span className="title">{title}</span>{role}{when}<span className="go">new task here →</span></div>
      </div>
    );
  }
  return <div className="story"><div className="line"><span className="title">{title}</span>{role}{when}</div></div>;
}

function Byline({ orch, now }: { orch: Surface[]; now: number }) {
  return <>{orch.map((sf, i) => <p key={i} className="orch">orchestrated by <b>{s(sf.title)}</b>{` · ${relTime(sf.opened || 0, now)}`}</p>)}</>;
}

/** render_fleet for the project page: the stale-code line, then the project's section. */
function Running({ d, now }: { d: ProjectPayload; now: number }) {
  const p = d.project;
  const tree = clean(d.tree);
  const c: RowCtx = { d, now, tree, kids: kidsOf(tree) };
  // render_fleet folds the rows again without the link graph, which leaves no
  // earlier rounds on them: the project page shows no "N earlier rounds" links
  const sec = projectSection(p, d.mine, collapse(d.rows));
  const stale = staleCode({ fleet_page_code: d.fleet_page_code }, now);
  return (
    <>
      {stale && <section className="card stale"><p><span className="badge needs">stale code</span>{` ${stale}`}</p></section>}
      <section className="card project durable">
        <h2>
          <a className="repo" href={`/project/${p.id}`}>{p.name}</a>
          {sec.live ? <span className="badge">{`${sec.live} running`}</span> : null}
          {sec.done ? <a className="hist" href={`/project/${p.id}#finished`}>{`${sec.done} finished`}</a> : null}
          <a className="hist" href={`/project/${p.id}#launch`}>new work →</a>
        </h2>
        {sec.parts.map((part, i) => i === 0 ? (
          <Fragment key={part.project.id}><Byline orch={part.byline} now={now} />{part.rows.map((sf, j) => <Row key={j} sf={sf} c={c} />)}</Fragment>
        ) : (
          <div key={part.project.id} className="member">
            <h3><a href={`/project/${part.project.id}`}>{part.project.name}</a></h3>
            <Byline orch={part.byline} now={now} />{part.rows.map((sf, j) => <Row key={j} sf={sf} c={c} />)}
            {part.rows.length ? null : <p className="empty">nothing running</p>}
          </div>
        ))}
        {sec.live || sec.parts.length > 1 ? null : <p className="empty">Nothing running in this project.</p>}
      </section>
    </>
  );
}

/** The project screen. */
export function ProjectScreen({ id }: { id: string }) {
  const server = useServer();
  const { data: d, error } = useOnce(() => server.fetchProject(id), server.base + id);
  const now = Date.now() / 1000;
  const notice = new URLSearchParams(window.location.search).get('notice') || '';
  if (!d) return <Screen name={id} tab={`${id} — project`}><Pending error={error} /></Screen>;
  const p = d.project;
  const meta = [<a key="f" href="/">← fleet</a>];
  if (d.group) meta.push(<Fragment key="g">in <a href={`/project/${d.group.id}`}>{d.group.name}</a></Fragment>);
  if (d.members.length) {
    meta.push(<Fragment key="m">{'group of '}{d.members.map((m, i) => (
      <Fragment key={m.id}>{i ? ', ' : ''}<a href={`/project/${m.id}`}>{m.name}</a></Fragment>))}</Fragment>);
  }
  if (p.archived) meta.push(<b key="a">archived</b>);
  const dirs = [p, ...d.members].flatMap((m) => m.dirs || []);
  const defaults: string[] = [];
  if (p.skills?.length) defaults.push('default skills: ' + p.skills.join(', '));
  if (p.launch) defaults.push(`default launch: ${p.launch}`);
  const ctx = d.context;
  return (
    <Screen name={p.name} tab={`${p.name} — project`}>
      <p className="meta">{meta.map((m, i) => <Fragment key={i}>{i ? ' · ' : ''}{m}</Fragment>)}</p>
      {notice && <section className="card"><p>{notice}</p></section>}
      <section className="card" id="launch"><h2>new work</h2>
        {p.archived ? <p className="empty">Archived — restore it to launch work here.</p>
          : !d.where ? <p className="empty">This project has no directory to run work in.</p> : (
            <form className="newtask" method="post" action={`/projects/${p.id}/tasks`}>
              <textarea name="text" required placeholder="What should run in this project?"></textarea>
              <div className="row">
                <select name="cwd" defaultValue={d.where}>
                  {[...new Set(dirs)].map((dir) => <option key={dir} value={dir}>{dir}</option>)}
                </select>
                <button type="submit">Launch</button>
                <button type="submit" name="node" value="tutor">Study it</button>
              </div>
              <RunPicker lib={runLibrary(d.library)} current={p.launch || ''} />
              <p className="meta">everything installed: <a href="/library">library</a></p>
            </form>
          )}
      </section>
      <section className="card project"><h2>running</h2>
        {d.starting.map((st) => (
          <div key={st.task} className="rowline"><span className="row starting">
            <span className="title">{taskLabel(st.task)}</span>
            <span className="badge running">{startingWhat(st, now, d.doing)}</span>
            <span className="badge">{relTime(st.since || 0, now)}</span>
          </span></div>
        ))}
        {d.rows.length ? <Running d={d} now={now} /> : d.starting.length ? null : <p className="empty">Nothing running.</p>}
      </section>
      <section className="card" id="finished"><h2>{`finished · ${d.closed.length}`}</h2>
        {d.closed.length ? d.closed.slice(0, 100).map((m) => (
          <div key={m.key} className="rowline"><a className="row" href={`/view/${m.key}`}>
            <span className="title">{s(m.title || m.task || m.key)}</span>
            <span className="badge">{relTime(m.opened || 0, now)}</span>
            <span className="go">open →</span></a></div>
        )) : <p className="empty">Nothing finished yet.</p>}
      </section>
      <section className="card"><h2>context</h2>
        {ctx ? (
          <>
            <p className="meta">{ctx.path}</p>
            {ctx.brief ? <pre className="brief">{ctx.brief}</pre> : <p className="empty">No brief.md or README.md in it yet.</p>}
            {ctx.decisions.length ? (
              <details className="fold"><summary>{`${ctx.decisions.length} recorded decisions`}</summary>
                {ctx.decisions.map((n) => <p key={n} className="meta">{n}</p>)}</details>
            ) : <p className="empty">No decisions recorded yet.</p>}
          </>
        ) : <p className="empty">No context store. Point one at a folder holding brief.md and decisions/: <code>{`pipeline.py project set ${p.id} --context PATH`}</code></p>}
        <details className="fold"><summary>what a task launched here is told</summary><pre className="brief">{d.prompt}</pre></details>
      </section>
      <section className="card"><h2>project</h2>
        {defaults.map((t, i) => <p key={`d${i}`} className="meta">{t}</p>)}
        {dirs.map((t, i) => <p key={`r${i}`} className="meta">{t}</p>)}
        <form className="repair" method="post" action={`/projects/${p.id}/archive`}>
          <input type="hidden" name="archived" value={p.archived ? '0' : '1'} />
          <button type="submit" className={p.archived ? undefined : 'danger'}>{p.archived ? 'Restore' : 'Archive'}</button>
          <p>Archiving hides the project from the fleet and keeps its record.</p>
        </form>
      </section>
    </Screen>
  );
}
