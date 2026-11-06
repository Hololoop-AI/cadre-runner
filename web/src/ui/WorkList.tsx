// The Work list's card (statusd._items_html's body as the page script leaves
// it): the listed items, the "Nothing matches" line, the Idle fold, and the
// items off the list, kept in the DOM where find can still reach them.

import { staleCode, type Fleet, type Home, type Item, type Sel } from '../model';
import { ItemView } from './Item';
import type { WorkLayout } from './useWorkLayout';

export interface WorkListProps {
  home: Home;
  fleet: Fleet;
  work: WorkLayout;
  sel: Sel;
  /** the find box, which keeps the Idle fold open while it has text */
  q: string;
  /** the server's limit, drawn on the card as data-limit */
  serverLimit: number;
  /** keys of the items open now */
  open: Set<string>;
  onToggle: (key: string, open: boolean) => void;
  onNewWork: (parent: string) => void;
  /** the Idle fold opened or closed by hand */
  onIdleOpen: (open: boolean) => void;
  /** bumped whenever the Python page would have redrawn every item's inside */
  drawn: number;
}

export function WorkList({ home, fleet, work, sel, q, serverLimit, open, onToggle, onNewWork, onIdleOpen, drawn }: WorkListProps) {
  const { placed, textRef } = work;
  const item = (it: Item, offlist = false) => (
    <ItemView key={it.key} it={it} ctx={home.ctx} drawn={drawn} offlist={offlist} open={open.has(it.key)}
      onToggle={onToggle} onNewWork={onNewWork} textRef={textRef} />
  );
  const stale = staleCode(fleet, home.ctx.now);
  const find = q.trim();
  return (
    <div className="backend">
      {stale && (
        <section className="card stale"><p><span className="badge needs">stale code</span>{` ${stale}`}</p></section>
      )}
      <div className="card items" data-limit={serverLimit}>
        {placed.list.map((it) => item(it))}
        <p className="empty noitems" hidden={!!placed.list.length || !!placed.idle.length}>Nothing matches. Clear a filter above.</p>
        <details className="idlefold" data-fold={`${home.ctx.fold}|idle`} hidden={!placed.idle.length}
          open={!!placed.idle.length && (sel.idleOpen || !!find)}
          onToggle={(e) => {
            const now = (e.currentTarget as HTMLDetailsElement).open;
            if (!find && now !== sel.idleOpen) onIdleOpen(now);
          }}>
          <summary className="foldrow">Idle: <span className="n">{placed.idle.length}</span> more, least
            recently touched. They fold here only because the home limit is reached.</summary>
          <div className="idlebox">{placed.idle.map((it) => item(it))}</div>
        </details>
        <div className="offbox">{placed.off.map((it) => item(it, true))}</div>
      </div>
    </div>
  );
}
