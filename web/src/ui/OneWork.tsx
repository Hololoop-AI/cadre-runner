// A cadre's one Work list (model/work.ts workRows): the board's active cards
// and the few runs on no card, in a single list ordered by what needs you,
// then what an agent is on, then the rest. Cards are drawn as on the Kanban
// tab (CardBlock), closed until opened; a run on no card is a Work-list item
// (ItemView) marked "no card". Loose runs quiet for over a week fold below.
// The items kept off the list stay in the DOM, hidden, for find to reach.

import { useEffect, useMemo, useRef, useState } from 'react';
import { columnLabel, own, relTime, runsOnCards, staleCode, usd, workRows, type Fleet, type Home, type Item, type Kanban, type WorkRow, type WorkRows, type WorkView } from '../model';
import { CardBlock, cardBadge, tallyText } from './CardWork';
import { ItemView, stateBadge } from './Item';
import type { WorkLayout } from './useWorkLayout';

/** "2 need you · 1 working", or that nothing does. */
export function workSummary(w: Pick<WorkRows, 'needs' | 'working'>): string {
  const parts = [w.needs && `${w.needs} need${w.needs === 1 ? 's' : ''} you`, w.working && `${w.working} working`].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'nothing waits on you';
}

/** How the list is drawn: a grid of cards that open large, or one row each. */
export type WorkMode = 'cards' | 'list';

/** One tile of the grid: what the row says, with room for the card's note; a click opens it large. */
function Tile({ r, home, onOpen }: { r: WorkRow; home: Home; onOpen: (key: string) => void }) {
  const now = home.ctx.now;
  const name = (p: string) => own(home.ctx.names, p) ?? p;
  if (r.kind === 'card') {
    const { card, tally: t, facts } = r;
    return (
      <button type="button" className="wtile" data-card={card.id} onClick={() => onOpen(r.key)} aria-haspopup="dialog">
        <span className="ttop">{cardBadge(t)}{t.fresh > 0 && <span className="unseen" title="a run on it came back and you have not opened it"></span>}
          <span className="when">{facts.returned ? relTime(facts.returned, now) : 'nothing back yet'}</span></span>
        <span className="ttitle">{card.title}</span>
        {card.note && <span className="tnote">{card.note.split('\n')[0]}</span>}
        <span className="tfoot"><span className="wtally">{[columnLabel(card.column), tallyText({ ...t, yourTurn: 0, working: 0 })].join(' · ')}</span>
          {facts.projects.slice(0, 3).map((p) => <span key={p} className="proj">{name(p)}</span>)}
          {facts.spent ? <span className="badge cost">{usd(facts.spent)}</span> : null}</span>
      </button>
    );
  }
  const it = r.item;
  return (
    <button type="button" className="wtile loose" data-item={it.key} onClick={() => onOpen(r.key)} aria-haspopup="dialog">
      <span className="ttop">{stateBadge(it)}{it.unopened && <span className="unseen"></span>}
        <span className="when">{relTime(it.returned || it.touched, now)}</span></span>
      <span className="ttitle">{it.title}</span>
      <span className="tnote">On no card.</span>
      <span className="tfoot"><span className="proj">{name(it.project)}</span>
        {it.spent ? <span className="badge cost">{usd(it.spent)}</span> : null}</span>
    </button>
  );
}

/** The opened card or run, large, above everything: a modal dialog that Escape, the ✕ or a click outside closes. */
function Large({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (typeof d.showModal === 'function') { if (!d.open) d.showModal(); } else d.setAttribute('open', '');
    return () => { if (typeof d.close === 'function' && d.open) d.close(); };
  }, []);
  return (
    <dialog ref={ref} className="wlarge" aria-label={title} onClose={onClose}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="wlbody">
        <button type="button" className="wlclose" aria-label="Close" onClick={onClose}>✕</button>
        {children}
      </div>
    </dialog>
  );
}

export function OneWork({ board, fleet, home, work, view, q, cadre, folder, open, onToggle, onNewWork, drawn, onChanged, mode = 'list' }: {
  /** the board when this cadre owns it, else null */
  board: Kanban | null;
  fleet: Fleet; home: Home;
  /** the runs on no card, laid out by the Work list's rules */
  work: WorkLayout;
  /** sort, project and the state chips, applied to the cards as to the runs */
  view: WorkView;
  q: string; cadre: string; folder: string;
  open: Set<string>;
  onToggle: (key: string, open: boolean) => void;
  onNewWork: (parent: string) => void;
  drawn: number;
  onChanged: (msg: string) => void;
  mode?: WorkMode;
}) {
  const { placed, textRef } = work;
  const [large, setLarge] = useState<string | null>(null);
  const loose = useMemo(() => [...placed.list, ...placed.idle], [placed]);
  const byKey = useMemo(() => new Map(home.items.map((it) => [it.key, it])), [home]);
  const w = useMemo(() => workRows(board, fleet, loose, home.ctx.now, byKey, view), [board, fleet, loose, home.ctx.now, byKey, view]);
  const pages = useMemo(() => home.items.flatMap((it) => it.pages), [home]);
  const onCards = useMemo(() => runsOnCards(board), [board]);
  const find = q.trim().toLowerCase();
  const item = (it: Item, offlist = false) => (
    <ItemView key={it.key} it={it} ctx={home.ctx} drawn={drawn} offlist={offlist} open={open.has(it.key)}
      onToggle={onToggle} onNewWork={onNewWork} textRef={textRef} />
  );
  const names = (ps: string[]) => ps.map((p) => home.ctx.names?.[p] || p).join('\n');
  const rows = w.rows.filter((r) => r.kind === 'item' || !find
    || `${r.card.title}\n${r.card.note}\n${names(r.facts.projects)}`.toLowerCase().includes(find));
  const stale = staleCode(fleet, home.ctx.now);
  return (
    <div className="backend onework">
      {stale && <section className="card stale"><p><span className="badge needs">stale code</span>{` ${stale}`}</p></section>}
      {mode === 'cards' && (
        <div className="wtiles">{rows.map((r) => <Tile key={r.key} r={r} home={home} onOpen={setLarge} />)}</div>
      )}
      {mode === 'cards' && (() => {
        const r = rows.find((x) => x.key === large) || w.rows.find((x) => x.key === large);
        if (!r) return null;
        return (
          <Large title={r.kind === 'card' ? r.card.title : r.item.title} onClose={() => setLarge(null)}>
            <div className="card items">
              {r.kind === 'card'
                ? <CardBlock card={r.card} fleet={fleet} home={home} pages={pages} onCards={onCards} cadre={cadre}
                    folder={folder} open autoOpen={false} facts={r.facts} onChanged={onChanged} />
                : <ItemView it={r.item} ctx={home.ctx} drawn={drawn} open onToggle={() => {}} onNewWork={onNewWork} />}
            </div>
          </Large>
        );
      })()}
      <div className={mode === 'cards' && !w.older.length ? 'card items wempty' : 'card items'}>
        {mode === 'list' && rows.map((r) => (r.kind === 'card'
          ? <CardBlock key={r.key} card={r.card} fleet={fleet} home={home} pages={pages} onCards={onCards} cadre={cadre}
              folder={folder} open={false} autoOpen={false} facts={r.facts} onChanged={onChanged} />
          : item(r.item)))}
        <p className="empty noitems" hidden={!!rows.length || !!w.older.length}>
          {find ? 'Nothing matches. Clear find or a filter.' : board ? 'Nothing in hand. Start a card on the Kanban page.' : 'Nothing in hand.'}</p>
        {w.older.length > 0 && (
          <details className="idlefold" open={!!find || undefined}>
            <summary className="foldrow">{`Older, on no card: ${w.older.length} quiet for over a week. Put one on a card, or archive it.`}</summary>
            <div className="idlebox">{w.older.map((it) => item(it))}</div>
          </details>
        )}
        <div className="offbox">{placed.off.map((it) => item(it, true))}</div>
      </div>
    </div>
  );
}
