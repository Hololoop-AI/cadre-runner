// /page/<key>: statusd.render_page — one conversation's rounds, newest
// first, and the four repairs: move it, mark it replaced, end its session,
// attach an agent.

import { escapedCut, relTime, s, type PageOwner } from '../model';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

/** statusd._attach_form: attach an agent to a page nobody owns; any other owner says why not. */
function Attach({ head, owner }: { head: string; owner: PageOwner | null }) {
  const o = owner || { state: 'unknown' };
  const why: Record<string, string> = {
    task: `Owned by dialogue ${o.task}: annotate the page and it reaches that agent as its next turn.`,
    pipeline: `Owned by the pipeline (${o.kind} session): the runner already delivers its feedback.`,
    pending: 'An attach is queued; the runner dispatches it on its next tick.',
    outside: 'An agent outside the runner is working on this page right now; attaching would give it a second owner.',
    unknown: 'Not an open runner session with a page file, so nothing can be attached from here' + (o.why ? ` (${o.why}).` : '.'),
  };
  if (Object.prototype.hasOwnProperty.call(why, o.state)) {
    return <form className="repair"><button type="submit" disabled>Attach agent</button><p>{why[o.state]}</p></form>;
  }
  return (
    <form className="repair" method="post" action="/repair">
      <input type="hidden" name="key" value={head} />
      <input type="hidden" name="action" value="attach" />
      <input name="instruction" maxLength={4000} placeholder="instruction for the agent (optional)" />
      <button type="submit">Attach agent</button>
      <p>{'No agent owns this page. Dispatches a dialogue that owns it from now on and rewrites this same page; with no instruction it reads the page and continues it.'
        + (o.error ? ` Last attach failed: ${o.error}.` : '')}</p>
    </form>
  );
}

/** The manage screen for the page `pageKey` (any round of its conversation). */
export function PageScreen({ pageKey }: { pageKey: string }) {
  const server = useServer();
  const { data: d, error } = useOnce(() => server.fetchPage(pageKey), server.base + pageKey);
  const now = Date.now() / 1000;
  const notice = new URLSearchParams(window.location.search).get('notice') || '';
  if (!d) return <Screen name={pageKey} dot={false} tab={`${pageKey} — manage`}><Pending error={error} /></Screen>;
  const row = d.row;
  const head = s(row.path).slice(s(row.path).lastIndexOf('/') + 1);
  const title = s(row.title || head);
  const rounds = [{ key: head, title: s(row.page_title || row.title || head), opened: row.opened || 0 }, ...(row.earlier || [])];
  const ended = !!row.ended;
  return (
    <Screen name={title} dot={false} tab={`${title} — manage`}>
      <p className="meta"><a href="/">← fleet</a>{' · project '}<b>{s(row.project || 'none')}</b>{` · page ${head}`}</p>
      {notice && <section className="card"><p>{notice}</p></section>}
      {pageKey === head ? null : <p className="meta">{`you opened an earlier round (${pageKey}); the conversation continues in the newest one`}</p>}
      <section className="card"><h2>{`this conversation · ${rounds.length} round${rounds.length !== 1 ? 's' : ''}, newest first`}</h2>
        {rounds.map((r, i) => (
          <div key={r.key} className="rowline"><a className="row" href={`/view/${r.key}`}>
            <span className="title">{s(r.title)}</span>
            {i === 0 && <span className="chip open">newest</span>}
            <span className="badge">{relTime(r.opened || 0, now)}</span>
            <span className="go">open →</span></a></div>
        ))}
      </section>
      <section className="card"><h2>repairs</h2>
        <form className="repair" method="post" action="/repair">
          <input type="hidden" name="key" value={head} />
          <input type="hidden" name="action" value="move" />
          <input name="project" list="projects" required placeholder="move to project…" defaultValue={s(row.project || '')} />
          <datalist id="projects">{d.projects.map((p) => <option key={p} value={p} />)}</datalist>
          <button type="submit">Move</button>
          <p>Files this conversation under another project. Recorded as a child-of link.</p>
        </form>
        <form className="repair" method="post" action="/repair">
          <input type="hidden" name="key" value={head} />
          <input type="hidden" name="action" value="replace" />
          <select name="target" required defaultValue="">
            <option value="">replaced by…</option>
            {d.candidates.filter((c) => c.key !== head).map((c) => (
              <option key={c.key} value={c.key}>{escapedCut(s(c.title), 90)}</option>
            ))}
          </select>
          <button type="submit">Mark replaced</button>
          <p>Folds this conversation into the page that replaces it; it stays reachable as an earlier round. Recorded as a supersedes link, refused if it would loop.</p>
        </form>
        <form className="repair" method="post" action="/repair">
          <input type="hidden" name="key" value={head} />
          <input type="hidden" name="action" value="end" />
          <button type="submit" className="danger" disabled={ended}>End session</button>
          <p>{ended ? 'This session has already ended.' : 'For a stuck session: ends it in review-surface and marks it closed for the runner.'}</p>
        </form>
        <Attach head={head} owner={d.owner} />
      </section>
    </Screen>
  );
}
