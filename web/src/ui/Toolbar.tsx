// The Work list's controls (statusd.work_toolbar), the state and tag chips
// the page script fills, and Find everywhere's results (render_found).

import { CHIPS, relTime, SORTS, viewHref, type Hit, type Sel } from '../model';

export interface ToolbarProps {
  q: string;
  setQ: (q: string) => void;
  sort: string;
  setSort: (s: string) => void;
  proj: string;
  setProj: (p: string) => void;
  limit: number;
  limits: number[];
  setLimit: (n: number) => void;
  projects: [string, string][];
}

export function Toolbar(p: ToolbarProps) {
  return (
    <form className="toolbar" id="workbar" method="get" action="/" onSubmit={(e) => e.preventDefault()}>
      <input id="filter" type="search" name="q" aria-label="find" autoComplete="off"
        placeholder="find: filters these items and searches every page ever opened"
        value={p.q} onChange={(e) => p.setQ(e.target.value)} />
      <select id="sort" name="sort" aria-label="sort" value={p.sort} onChange={(e) => p.setSort(e.target.value)}>
        {Object.entries(SORTS).map(([k, v]) => <option key={k} value={k}>{`Sort: ${v}`}</option>)}
      </select>
      <select id="proj" className="projfilter" aria-label="project" value={p.proj} onChange={(e) => p.setProj(e.target.value)}>
        <option value="">All projects</option>
        {p.projects.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
      </select>
      <label className="hint">Home shows <select id="limit" name="limit" aria-label="home limit" value={String(p.limit)}
        onChange={(e) => p.setLimit(+e.target.value)}>
        {p.limits.map((n) => <option key={n} value={String(n)}>{n}</option>)}
      </select> items, then the least recently touched fold into Idle</label>
    </form>
  );
}

/** The state chips and the tag chips (`tags` already includes a kept tag no item carries: tagChips). */
export function Chips({ sel, count, tags, onState, onTag }: {
  sel: Sel; count: Record<string, number>; tags: string[];
  onState: (st: string) => void; onTag: (t: string) => void;
}) {
  return (
    <>
      <div className="toolbar chips" id="states">
        {CHIPS.map(([st, label]) => {
          const on = st === 'unopened' ? sel.unopened : sel.show.includes(st);
          return (
            <button key={st} type="button" className="chipbtn" aria-pressed={on} onClick={() => onState(st)}>
              {st === 'unopened' && <span className="unseen"></span>}{`${st === 'unopened' ? ' ' : ''}${label} ${count[st] || 0}`}
            </button>
          );
        })}
      </div>
      <div className="toolbar chips" id="tags">
        {tags.length ? <span className="hint">tags:</span> : null}
        {tags.map((t) => (
          <button key={t} type="button" className="chipbtn" aria-pressed={sel.tag === t} onClick={() => onTag(t)}>{`#${t}`}</button>
        ))}
      </div>
    </>
  );
}

export function Found({ q, hits, now }: { q: string; hits: Hit[] | null; now: number }) {
  return (
    <section id="found" className="card" hidden={hits === null}>
      {hits === null ? null : !hits.length ? <h2>{`everywhere · no page matches “${q}”`}</h2> : (
        <>
          <h2>{`everywhere · ${hits.length} page${hits.length !== 1 ? 's' : ''} match “${q}”`}</h2>
          {hits.map((h) => (
            <div key={h.key} className="rowline">
              <a className="row" href={viewHref(h.path)}>
                <span className="title">{h.title}</span>
                <span className={`chip ${h.state}`}>{h.state}</span>
                {h.project && <span className="badge">{h.project}</span>}
                {h.updated ? <span className="badge">{relTime(h.updated, now)}</span> : null}
                <span className="go">open →</span>
              </a>
              <a className="hist" href={`/page/${h.key}`}>manage</a>
            </div>
          ))}
        </>
      )}
    </section>
  );
}
