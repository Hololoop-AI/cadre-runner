// The home page of the cadre that owns the kanban board: its work is the
// board's cards, not a list of runs. Each card being worked on (Doing, then
// Ready) folds open to the runs done for it — launched from the card, or
// attached later — with where each stands and when it came back, so a card
// is followed to done instead of its work being lost in a list. A run is
// started from its card. New ideas that spark from the work go to the
// Backlog in one line, so they are kept without becoming a tangent; the
// Backlog itself is on the Kanban tab, not here. Each card
// is drawn as a Work-list item: its runs as the graph that made them, and a
// box to start a run on it, empty card or not.

import { useState } from 'react';
import {
  byColumn, cardRuns, cardTally, columnLabel, own, pruneKids, relTime, runPages, usd,
  type CardFacts, type CardTally, type Fleet, type Home, type Kanban, type KanbanCard, type Surface,
} from '../model';
import { DirField } from './DirField';
import { ItemGraph } from './Item';
import { useServer } from './server';
import './kanban.css';

/** "3 runs · 1 your turn · 1 working · 1 done", leaving out what is zero. */
export function tallyText(t: CardTally): string {
  if (!t.runs) return 'no runs yet';
  return [`${t.runs} run${t.runs === 1 ? '' : 's'}`, t.yourTurn && `${t.yourTurn} your turn`,
    t.working && `${t.working} working`, t.done && `${t.done} done`, t.fresh && `${t.fresh} new`].filter(Boolean).join(' · ');
}

/** The card's state, as a run's row says it: Your turn, Agent working, or Idle. */
export function cardBadge(t: CardTally) {
  if (t.yourTurn) return <span className="badge you">{t.yourTurn > 1 ? `Your turn · ${t.yourTurn}` : 'Your turn'}</span>;
  if (t.working) return <span className="badge work">{t.working > 1 ? `${t.working} agents running` : 'Agent working'}</span>;
  return <span className="badge idle">{t.runs ? 'Idle' : 'Not started'}</span>;
}

/**
 * Start a run on the card itself: what the agent should do and the folder it
 * works in. The run is attached to the card, and a card not yet being worked
 * on moves to the board's working column (runnerlib/tasks.py submit_task).
 */
function CardStart({ card, cadre, folder, empty, onChanged }: {
  card: KanbanCard; cadre: string; folder: string; empty: boolean; onChanged: (msg: string) => void;
}) {
  const server = useServer();
  const [text, setText] = useState('');
  const [dir, setDir] = useState(folder);
  const [busy, setBusy] = useState(false);
  const start = (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    const body = new URLSearchParams({ text: text.trim(), card: card.id, cwd: dir.trim() });
    if (cadre) body.set('cadre', cadre);
    server.post('/tasks', body)
      .then(({ notice }) => { setText(''); onChanged(notice || 'Task submitted.'); })
      .catch((err: unknown) => onChanged(err instanceof Error ? err.message : String(err)))
      .finally(() => setBusy(false));
  };
  return (
    <details className="wstartfold" open={empty || undefined}>
      <summary className="btn ghost">Start work on this card</summary>
      <form className="wstart" onSubmit={start} aria-label={`Start a run for ${card.title}`}>
        <textarea name="text" rows={2} required value={text} onChange={(e) => setText(e.target.value)}
          aria-label={`What should the agent do for ${card.title}?`} placeholder="What should the agent do for this card?" />
        <DirField fid={`wdir-${card.id}`} lid={`wsug-${card.id}`} label="Folder it works in" name="cwd" value={dir} onChange={setDir} />
        <button type="submit" className="primary" disabled={busy}>Start</button>
      </form>
    </details>
  );
}

/**
 * One card, drawn as the Work list draws an item: a drop-down whose inside
 * is each of the card's runs as the tree that made it (Item.tsx ItemGraph),
 * then Start work on this card (folded, as + Tag is; open on a card with
 * no runs yet). A card with no runs is the same drop-down, empty.
 */
export function CardBlock({ card, fleet, home, pages, onCards, cadre, folder, open, onChanged, autoOpen = true, facts }: {
  card: KanbanCard; fleet: Fleet; home: Home; pages: Surface[]; cadre: string; folder: string;
  /** every run on any card (runsOnCards): a run's graph stops at another of them */
  onCards: ReadonlySet<string>;
  open: boolean; onChanged: (msg: string) => void;
  /** open on its own when a run on it came back unopened (the Kanban tab); the home's one list keeps every card closed */
  autoOpen?: boolean;
  /** the home's row facts (projects, last result, cost): drawn as a run's row draws them */
  facts?: CardFacts;
}) {
  const runs = cardRuns(card, fleet);
  const t = cardTally(runs);
  return (
    <details className="item wcard" data-card={card.id} open={open || (autoOpen && t.fresh > 0)}>
      {facts ? (
        <summary className="row">
          <span className="caret"></span>
          {t.fresh > 0 && <span className="unseen" title="a run on it came back and you have not opened it"></span>}
          <span className="title">{card.title}</span>
          <span className="stbadge">{cardBadge(t)}</span>
          <span className="wtally">{[columnLabel(card.column), tallyText({ ...t, yourTurn: 0, working: 0 })].join(' · ')}</span>
          {facts.projects.slice(0, 2).map((p) => <span key={p} className="proj">{own(home.ctx.names, p) ?? p}</span>)}
          {facts.spent ? <span className="badge cost" title="every run on this card">{usd(facts.spent)}</span> : null}
          <span className="when" title={facts.returned ? 'when a run on it last came back' : 'nothing has come back yet'}>
            {facts.returned ? relTime(facts.returned, home.ctx.now) : '—'}</span>
        </summary>
      ) : (
        <summary className="row">
          <span className="caret"></span>
          <span className="kc">{columnLabel(card.column)}</span>
          <span className="title">{card.title}</span>
          <span className={t.yourTurn ? 'wtally on' : 'wtally'}>{tallyText(t)}</span>
        </summary>
      )}
      <div className="inside">
        {card.note && <p className="meta wnote">{card.note.split('\n')[0]}</p>}
        <div className="grp">{runs.length ? 'Runs for this card, as the tree that made them' : 'Nothing has run for this card yet'}</div>
        {runs.map((r) => {
          // the run's own tree, less the sessions that are runs on a card of their own
          const kids = pruneKids(home.ctx.kids, new Set([...onCards].filter((k) => k !== r.key)));
          return (
            <div key={r.key} className="pages wrun" data-run={r.key}>
              <ItemGraph it={{ key: r.key, pages: runPages(r.key, pages, kids) }} ctx={{ ...home.ctx, kids }} />
            </div>
          );
        })}
        <CardStart card={card} cadre={cadre} folder={folder} empty={!runs.length} onChanged={onChanged} />
      </div>
    </details>
  );
}

/**
 * Under the home's one Work list: how many cards wait in the Backlog and are
 * done (a link to the board, where they are started), and a one-line add,
 * so an idea that sparks mid-work is parked without becoming a tangent.
 */
export function BacklogLine({ board, kanbanHref, onChanged }: { board: Kanban; kanbanHref: string; onChanged: (msg: string) => void }) {
  const server = useServer();
  const [idea, setIdea] = useState('');
  const cols = byColumn(board);
  const [first, last] = [board.columns[0], board.columns[board.columns.length - 1]];
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
    <form className="widea" onSubmit={add}>
      <input type="text" name="title" aria-label="A new idea for the backlog" value={idea} onChange={(e) => setIdea(e.target.value)}
        placeholder="Something sparked? Park it on the backlog" maxLength={200} />
      <button type="submit">Add to backlog</button>
      <a className="meta wbacklog" href={kanbanHref}>{`${backlog.length} in backlog`}{done.length ? ` · ${done.length} done` : ''}</a>
    </form>
  );
}
