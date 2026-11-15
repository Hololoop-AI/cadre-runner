// /kanban: the Board — runnerlib/kanban.py's cards in their columns. The
// driver adds, drags, edits and removes cards here; agents do the same with
// `pipeline.py kanban`. Every change, from either side, moves this machine's
// fleet version, so the fleet stream (/fleet/events) says `change` and the
// board re-reads itself: pushed, not polled. React only: there is no Python
// page to match, so its look is its own (kanban.css).

import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';
import type { Live } from '../api';
import { byColumn, cardItemHref, columnLabel, DRIVER, kanbanMove, taskLabel, type Kanban, type KanbanCard } from '../model';
import './kanban.css';
import { Pending, Screen } from './Screen';
import { useServer } from './server';

/** One change to the board, as POST /kanban takes it. */
export type BoardChange = Record<string, string> & { action: 'add' | 'move' | 'edit' | 'remove' };

const LIVE: Record<Live, string> = {
  connecting: 'connecting…',
  on: '● live',
  half: '● live (a machine is not answering; changes may lag)',
  off: '○ reconnecting; re-reading every 30 s until then',
};

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The Board screen: the chosen machine's board, kept current by its fleet stream. */
export function KanbanScreen() {
  const server = useServer();
  const [board, setBoard] = useState<Kanban | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(() => new URLSearchParams(window.location.search).get('notice') || '');
  const [live, setLive] = useState<Live>('connecting');
  // Only the newest read draws, and none while a change of ours is still on
  // its way: a read that left before it landed would undo a drag on screen.
  const reads = useRef(0);
  const sending = useRef(0);
  const refetch = useCallback(() => {
    const n = ++reads.current;
    const current = () => n === reads.current && sending.current === 0;
    server.fetchKanban()
      .then((b) => { if (current()) { setBoard(b); setError(''); } })
      .catch((e: unknown) => { if (current()) setError(message(e)); });
  }, [server]);
  useEffect(() => {
    refetch();
    return server.subscribe(refetch, setLive);
  }, [server, refetch]);

  const send = (change: BoardChange) => {
    // a drag lands at once; the server's answer, and the stream, confirm it
    if (change.action === 'move') setBoard((b) => b && kanbanMove(b, change.card, change.column, change.before || ''));
    sending.current += 1;
    server.post('/kanban', new URLSearchParams(change))
      .then(({ notice: said }) => setNotice(change.action === 'move' && !said.startsWith('Refused') ? '' : said))
      .catch((e: unknown) => setNotice(message(e)))
      .finally(() => {
        sending.current -= 1;
        refetch();
      });
  };

  return (
    <Screen name="Board" tab="Cadre — board">
      <p className="meta"><a href="/">← fleet</a>{' · '}<span className={`live ${live}`}>{LIVE[live]}</span>
        {' · agents use '}<code>pipeline.py kanban</code></p>
      {notice && <p className="notice">{notice}</p>}
      {!board ? <Pending error={error} /> : <Board board={board} onChange={send} />}
    </Screen>
  );
}

/** The columns side by side (stacked on a phone), each with its add box and cards. */
export function Board({ board, onChange }: { board: Kanban; onChange: (c: BoardChange) => void }) {
  const [dragged, setDragged] = useState('');
  const [editing, setEditing] = useState('');
  const cols = byColumn(board);
  const drop = (e: DragEvent, column: string, before: string) => {
    e.preventDefault();
    e.stopPropagation();
    const card = dragged || e.dataTransfer.getData('text/plain');
    setDragged('');
    if (card) onChange({ action: 'move', card, column, before });
  };
  return (
    <div className="kanban">
      {board.columns.map((column, i) => {
        const cards = cols.get(column) || [];
        return (
          <section key={column} className="kcol" data-column={column}
            onDragOver={(e) => e.preventDefault()} onDrop={(e) => drop(e, column, '')}>
            <h2>{columnLabel(column)} <span className="kn">{cards.length}</span></h2>
            <AddCard column={column} onChange={onChange} />
            <ol>
              {cards.map((c, j) => (
                <li key={c.id} className={c.id === dragged ? 'kcard dragging' : 'kcard'} draggable={editing !== c.id}
                  onDragStart={(e) => { e.dataTransfer.setData('text/plain', c.id); e.dataTransfer.effectAllowed = 'move'; setDragged(c.id); }}
                  onDragEnd={() => setDragged('')}
                  onDragOver={(e) => e.preventDefault()}
                  // the lower half of a card means "after it"
                  onDrop={(e) => drop(e, column, lowerHalf(e) ? cards[j + 1]?.id ?? '' : c.id)}>
                  {editing === c.id
                    ? <EditCard card={c} onChange={onChange} onDone={() => setEditing('')} />
                    : <CardFace card={c} prev={board.columns[i - 1]} next={board.columns[i + 1]}
                        onEdit={() => setEditing(c.id)} onChange={onChange} />}
                </li>
              ))}
            </ol>
          </section>
        );
      })}
    </div>
  );
}

function lowerHalf(e: DragEvent): boolean {
  const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
  return e.clientY > r.top + r.height / 2;
}

/** A card as it sits: its title opens the editor; ← and → move it a column (dragging is awkward on a phone). */
function CardFace({ card, prev, next, onEdit, onChange }: {
  card: KanbanCard; prev?: string; next?: string; onEdit: () => void; onChange: (c: BoardChange) => void;
}) {
  const move = (column: string) => onChange({ action: 'move', card: card.id, column, before: '' });
  return (
    <>
      <button type="button" className="ktitle" title="edit" onClick={onEdit}>{card.title}</button>
      {card.note && <p className="knote">{card.note}</p>}
      <div className="kfoot">
        {card.item && <a href={cardItemHref(card.item)}>{card.item.startsWith('page:') ? 'its page' : taskLabel(card.item)}</a>}
        {card.by !== DRIVER && <span className="kby" title={card.by}>{`by ${taskLabel(card.by)}`}</span>}
        <span className="sp"></span>
        <button type="button" className="ghost" disabled={!prev} aria-label={prev ? `Move to ${columnLabel(prev)}` : 'Move left'}
          onClick={() => prev && move(prev)}>←</button>
        <button type="button" className="ghost" disabled={!next} aria-label={next ? `Move to ${columnLabel(next)}` : 'Move right'}
          onClick={() => next && move(next)}>→</button>
      </div>
    </>
  );
}

/** A card opened: its title and note, saved together; or removed. */
function EditCard({ card, onChange, onDone }: { card: KanbanCard; onChange: (c: BoardChange) => void; onDone: () => void }) {
  return (
    <form className="kedit" onSubmit={(e) => {
      e.preventDefault();
      const f = new FormData(e.currentTarget);
      onChange({ action: 'edit', card: card.id, title: String(f.get('title') || ''), note: String(f.get('note') || '') });
      onDone();
    }}>
      <input name="title" defaultValue={card.title} aria-label="title" maxLength={200} required autoFocus />
      <textarea name="note" defaultValue={card.note} aria-label="note" rows={3} placeholder="a note" />
      <div className="kfoot">
        <button type="submit" className="primary">Save</button>
        <button type="button" className="ghost" onClick={onDone}>Cancel</button>
        <span className="sp"></span>
        <button type="button" className="ghost" onClick={() => { onChange({ action: 'remove', card: card.id }); onDone(); }}>Remove</button>
      </div>
    </form>
  );
}

function AddCard({ column, onChange }: { column: string; onChange: (c: BoardChange) => void }) {
  return (
    <form className="kadd" onSubmit={(e) => {
      e.preventDefault();
      const input = e.currentTarget.elements.namedItem('title') as HTMLInputElement;
      if (!input.value.trim()) return;
      onChange({ action: 'add', column, title: input.value });
      input.value = '';
    }}>
      <input name="title" placeholder={`+ add to ${columnLabel(column)}`} aria-label={`add a card to ${columnLabel(column)}`} maxLength={200} />
    </form>
  );
}
