// Learn, the study cadre: its Review tab (what you review on a schedule:
// questions and problems of any shape, stored as the deck's cards; once the
// Python /cards page) and the panel its home shows beside the work (what is
// due, topics, courses, debriefs and the sessions in progress). Both read GET
// /api/deck (statusd.deck_payload). Nothing here is about code: a topic is
// any subject an item's `source` names.

import { useEffect, useState, type FormEvent } from 'react';
import { formBody } from '../api';
import type { CadreAgent, Deck, DeckCard, StudySession } from '../model';
import { Pending, Screen } from './Screen';
import { useServer } from './server';

/** The deck, re-read after every keep, drop or study the tab posts. */
function useDeck(): { deck: Deck | null; error: string; reload: () => void } {
  const server = useServer();
  const [n, setN] = useState(0);
  const [state, setState] = useState<{ deck: Deck | null; error: string }>({ deck: null, error: '' });
  useEffect(() => {
    let live = true;
    server.fetchDeck().then((deck) => live && setState({ deck, error: '' }))
      .catch((e: unknown) => live && setState({ deck: null, error: e instanceof Error ? e.message : String(e) }));
    return () => { live = false; };
  }, [server, n]);
  return { ...state, reload: () => setN((x) => x + 1) };
}

function when(iso: string): string {
  return iso ? iso.slice(0, 16).replace('T', ' ') : '';
}

/** Learn's Review tab: what is due, the topics, a study box, the items in review and those waiting to be kept. */
export function ReviewScreen({ back }: { back: string }) {
  const server = useServer();
  const { deck, error, reload } = useDeck();
  const [notice, setNotice] = useState('');
  const send = (action: string) => (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const body = formBody(e.currentTarget, (e.nativeEvent as SubmitEvent).submitter);
    body.set('back', back);
    server.post(action, body).then((to) => { setNotice(to.notice); reload(); })
      .catch((err: unknown) => setNotice(err instanceof Error ? err.message : String(err)));
  };
  const keep = (c: DeckCard) => (
    <form method="post" action="/cards" className="repair" onSubmit={send('/cards')}>
      <input type="hidden" name="id" value={c.id} />
      <button name="do" value="keep">keep</button>
      <button name="do" value="drop" className="danger">drop</button>
    </form>
  );
  const table = (rows: DeckCard[], last: (c: DeckCard) => React.ReactNode) => (
    <table className="bboard deck">
      <thead><tr><th>to review</th><th>topic</th><th>kind</th><th /></tr></thead>
      <tbody>{rows.map((c) => (
        <tr key={c.id}><td>{c.front}</td><td>{c.source}{c.scope === 'team' ? ' · team' : ''}</td><td>{c.kind}</td><td>{last(c)}</td></tr>
      ))}</tbody>
    </table>
  );
  return (
    <Screen name="Review" tab="Learn — review">
      {notice && <p className="notice">{notice}</p>}
      {!deck ? <Pending error={error} /> : (
        <>
          {deck.notice && <p className="notice">{deck.notice}</p>}
          <p className="meta"><b>{deck.due.length}</b>{' due now · '}<b>{deck.review.length}</b>{' in review · '}
            <b>{deck.proposed.length}</b>{` waiting for you to keep or drop · ${deck.discarded} dropped. `}
            Questions and problems of any shape, on any subject: study sessions and threads propose them, and they come due inside sessions.</p>
          <section aria-label="Study a topic">
            <h3>Study a topic</h3>
            <form method="post" action="/study" className="newtask" onSubmit={send('/study')}>
              <textarea name="topic" required placeholder="What do you want to learn?" />
              <div className="row">
                {deck.dirs.length > 0 && (
                  <select name="cwd" aria-label="The folder holding the material">
                    {deck.dirs.map((d) => <option key={d} value={d}>{d}</option>)}
                  </select>
                )}
                <button type="submit">Study it</button>
              </div>
              <p className="meta">A study session in Learn: what is due first, one short idea, a few questions, graded next round.
                It reads the material in the folder you pick; it never changes it.</p>
            </form>
          </section>
          {deck.topics.length > 0 && (
            <section aria-label="Topics"><h3>Topics</h3>
              <p className="meta">{deck.topics.map((t) => `${t.source} (${t.cards}${t.due ? `, ${t.due} due` : ''})`).join(' · ')}</p>
            </section>
          )}
          {deck.proposed.length > 0 && <section aria-label="Proposed"><h3>Proposed</h3>{table(deck.proposed, keep)}</section>}
          {deck.review.length > 0 && <section aria-label="In review"><h3>In review · soonest due first</h3>{table(deck.review, (c) => when(c.due_at))}</section>}
        </>
      )}
    </Screen>
  );
}

function sessionLine(s: StudySession) {
  const from = s.askers.filter(Boolean).join(', ');
  return (
    <li key={s.session}><a href={s.page}>{s.material}</a>
      <span className="meta">{` ${s.purposes.join(', ')}${from ? ` · asked by ${from}` : ''}${s.ready ? ' · ready' : ''}`}</span></li>
  );
}

/** What Learn's home shows beside its work: what is due, topics, courses, debriefs on offer and sessions in progress. */
export function LearnPanel({ reviewHref }: { reviewHref: string }) {
  const { deck, error } = useDeck();
  if (!deck) return error ? <section className="needs"><p className="meta">{`The deck could not be read: ${error}`}</p></section> : null;
  const open = deck.sessions.filter((s) => s.open);
  const courses = deck.sessions.filter((s) => s.purposes.includes('course'));
  return (
    <section className="needs" aria-label="Learning">
      <h2 className="sect">Due <span className="kn">{deck.due.length}</span></h2>
      {!deck.due.length ? <p className="meta">Nothing due. </p> : (
        <ul>{deck.due.slice(0, 5).map((c) => <li key={c.id}><a href={reviewHref}>{c.front}</a></li>)}</ul>
      )}
      {deck.proposed.length > 0 && <p className="meta"><a href={reviewHref}>{`${deck.proposed.length} proposed, waiting for you to keep or drop`}</a></p>}
      <h2 className="sect">Topics <span className="kn">{deck.topics.length}</span></h2>
      {!deck.topics.length ? <p className="meta">Nothing kept for review yet.</p>
        : <p className="meta">{deck.topics.slice(0, 8).map((t) => `${t.source} (${t.cards}${t.due ? `, ${t.due} due` : ''})`).join(' · ')}</p>}
      <h2 className="sect">Sessions in progress <span className="kn">{open.length}</span></h2>
      {!open.length ? <p className="meta">None open. A page, a thread or another cadre asks for one; so does the Review tab.</p>
        : <ul>{open.slice(0, 6).map(sessionLine)}</ul>}
      <h2 className="sect">Courses <span className="kn">{courses.length}</span></h2>
      {!courses.length ? <p className="meta">No course yet. Ask for a study with the purpose course to start one.</p>
        : <ul>{courses.slice(0, 6).map(sessionLine)}</ul>}
      <h2 className="sect">Debriefs on offer</h2>
      <p className="meta">Offered in a thread at the end of a large piece of delegated work; none is tracked here yet.</p>
    </section>
  );
}

/** A cadre's Agents tab when its record lists them (Learn's tutor, course builder and card writer). */
export function AgentsList({ name, agents }: { name: string; agents: CadreAgent[] }) {
  return (
    <section>
      <h2 className="sect">Agents</h2>
      <p className="meta">{`${name}'s agents and the skill each works with. Their evals arrive with the agents view.`}</p>
      <table className="bboard agents">
        <thead><tr><th>agent</th><th>skill</th><th>what it does</th></tr></thead>
        <tbody>{agents.map((a) => <tr key={a.id}><td>{a.name}</td><td><code>{a.skill}</code></td><td>{a.does}</td></tr>)}</tbody>
      </table>
    </section>
  );
}
