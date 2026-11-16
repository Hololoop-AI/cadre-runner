// The home page of the cadre that owns the kanban board: its work is the
// board's cards, not a list of runs. Each card being worked on (Doing, then
// Ready) folds open to the runs done for it — launched from the card, or
// attached later — with where each stands and when it came back, so a card
// is followed to done instead of its work being lost in a list. A run is
// started from its card. New ideas that spark from the work go to the
// Backlog in one line, so they are kept without becoming a tangent.

import { useState } from 'react';
import {
  activeCards, byColumn, cardRuns, cardTally, columnLabel, LABELS, relTime,
  type CardRun, type CardTally, type Fleet, type Kanban, type KanbanCard,
} from '../model';
import { useServer } from './server';
import './kanban.css';

/** Each state's badge, as the Work list draws it. */
const BADGE: Record<string, string> = { 'your-turn': 'badge you', working: 'badge work', idle: 'badge idle', done: 'badge done', hidden: 'badge idle' };

/** "3 runs · 1 your turn · 1 working · 1 done", leaving out what is zero. */
export function tallyText(t: CardTally): string {
  if (!t.runs) return 'no runs yet';
  return [`${t.runs} run${t.runs === 1 ? '' : 's'}`, t.yourTurn && `${t.yourTurn} your turn`,
    t.working && `${t.working} working`, t.done && `${t.done} done`].filter(Boolean).join(' · ');
}

function RunRow({ it, now }: { it: CardRun; now: number }) {
  return (
    <li className={it.state === 'your-turn' ? 'wrun you' : 'wrun'}>
      <span className={BADGE[it.state] || 'badge'}>{LABELS[it.state]}</span>
      <a href={it.href}>{it.title}</a>
      <span className="when">{it.returned ? `returned ${relTime(it.returned, now)}` : relTime(it.touched, now)}</span>
    </li>
  );
}

function CardBlock({ card, fleet, now, onRun }: { card: KanbanCard; fleet: Fleet; now: number; onRun: (c: KanbanCard) => void }) {
  const runs = cardRuns(card, fleet);
  const t = cardTally(runs);
  return (
    <details className="wcard" data-card={card.id} open={card.column === 'doing' || t.yourTurn > 0}>
      <summary>
        <span className="kc">{columnLabel(card.column)}</span>
        <span className="kt">{card.title}</span>
        <span className={t.yourTurn ? 'wtally on' : 'wtally'}>{tallyText(t)}</span>
      </summary>
      {card.note && <p className="meta wnote">{card.note.split('\n')[0]}</p>}
      {runs.length ? <ul className="wruns">{runs.map((it) => <RunRow key={it.key} it={it} now={now} />)}</ul>
        : <p className="meta">Nothing has run for this card yet.</p>}
      <p className="wact"><button type="button" onClick={() => onRun(card)}>+ Run for this card</button></p>
    </details>
  );
}

/**
 * The board's work: Doing and Ready cards with their runs, then the Backlog
 * (folded, with a one-line add) and how many are done. `items` are the
 * fleet payload each run is read from by its own task; `onRun` opens New work for a card; `onChanged`
 * re-reads after an add.
 */
export function CardWork({ board, fleet, now, kanbanHref, onRun, onChanged }: {
  board: Kanban; fleet: Fleet; now: number; kanbanHref: string;
  onRun: (c: KanbanCard) => void; onChanged: (msg: string) => void;
}) {
  const server = useServer();
  const [idea, setIdea] = useState('');
  const cols = byColumn(board);
  const [first, last] = [board.columns[0], board.columns[board.columns.length - 1]];
  const active = activeCards(board);
  const backlog = cols.get(first) || [];
  const done = cols.get(last) || [];
  const add = (e: React.FormEvent) => {
    e.preventDefault();
    const title = idea.trim();
    if (!title) return;
    server.post('/kanban', new URLSearchParams({ action: 'add', column: first, title }))
      .then(({ notice }) => { setIdea(''); onChanged(notice); })
      .catch((err: unknown) => onChanged(err instanceof Error ? err.message : String(err)));
  };
  return (
    <section className="cwork" aria-label="Work on the board">
      <h2 className="sect">Work on the board <a href={kanbanHref}>open the board →</a></h2>
      {!active.length && <p className="meta">{`Nothing in ${board.columns.slice(1, -1).map(columnLabel).join(' or ')}. Move a card there to start on it.`}</p>}
      {active.map((c) => <CardBlock key={c.id} card={c} fleet={fleet} now={now} onRun={onRun} />)}
      <details className="wbacklog">
        <summary>{`Backlog · ${backlog.length}`}{done.length ? ` · ${done.length} done` : ''}</summary>
        {backlog.length > 0 && <ul>{backlog.map((c) => <li key={c.id}>{c.title}</li>)}</ul>}
      </details>
      <form className="widea" onSubmit={add}>
        <input type="text" name="title" aria-label="A new idea for the backlog" value={idea} onChange={(e) => setIdea(e.target.value)}
          placeholder="Something sparked? Park it on the backlog and keep going" maxLength={200} />
        <button type="submit">Add to backlog</button>
      </form>
    </section>
  );
}
