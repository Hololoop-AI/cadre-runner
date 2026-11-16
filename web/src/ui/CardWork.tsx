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

import { useMemo, useState } from 'react';
import {
  activeCards, byColumn, cardRuns, cardTally, columnLabel, pruneKids, runPages, runsOnCards,
  type CardTally, type Fleet, type Home, type Kanban, type KanbanCard, type Surface,
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
export function CardBlock({ card, fleet, home, pages, onCards, cadre, folder, open, onChanged }: {
  card: KanbanCard; fleet: Fleet; home: Home; pages: Surface[]; cadre: string; folder: string;
  /** every run on any card (runsOnCards): a run's graph stops at another of them */
  onCards: ReadonlySet<string>;
  open: boolean; onChanged: (msg: string) => void;
}) {
  const runs = cardRuns(card, fleet);
  const t = cardTally(runs);
  return (
    <details className="item wcard" data-card={card.id} open={open || t.fresh > 0}>
      <summary className="row">
        <span className="caret"></span>
        <span className="kc">{columnLabel(card.column)}</span>
        <span className="title">{card.title}</span>
        <span className={t.yourTurn ? 'wtally on' : 'wtally'}>{tallyText(t)}</span>
      </summary>
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
 * The board's work: Doing and Ready cards with their runs, then how many
 * wait in the Backlog (a link to the board, where they are started), a
 * one-line add, and how many are done. `home` is what each run's graph is
 * drawn from; `cadre` and `folder` are what a run started on a card gets;
 * `onChanged` re-reads after an add or a start.
 */
export function CardWork({ board, fleet, home, kanbanHref, cadre, folder, onChanged }: {
  board: Kanban; fleet: Fleet; home: Home; kanbanHref: string; cadre: string; folder: string;
  onChanged: (msg: string) => void;
}) {
  const server = useServer();
  const pages = useMemo(() => home.items.flatMap((it) => it.pages), [home]);
  const onCards = useMemo(() => runsOnCards(board), [board]);
  const block = (c: KanbanCard, open: boolean) => (
    <CardBlock key={c.id} card={c} fleet={fleet} home={home} pages={pages} onCards={onCards} cadre={cadre} folder={folder} open={open} onChanged={onChanged} />
  );
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
      {active.length > 0 && <div className="card items">{active.map((c) => block(c, false))}</div>}
      <p className="meta wbacklog"><a href={kanbanHref}>{`Backlog · ${backlog.length}`}{done.length ? ` · ${done.length} done` : ''}</a>
        {' on the board, where any card opens to start work on it.'}</p>
      <form className="widea" onSubmit={add}>
        <input type="text" name="title" aria-label="A new idea for the backlog" value={idea} onChange={(e) => setIdea(e.target.value)}
          placeholder="Something sparked? Park it on the backlog and keep going" maxLength={200} />
        <button type="submit">Add to backlog</button>
      </form>
    </section>
  );
}
