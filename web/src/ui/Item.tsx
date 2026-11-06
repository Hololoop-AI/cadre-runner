// One Work list item: a <details> drawn exactly as statusd.py's
// _items_html draws it (same elements, same classes), so fleet.css — the
// Python page's own stylesheet — styles both the same.

import { memo, useCallback, useMemo, useState, type ReactNode } from 'react';
import './beyond.css';
import {
  answered, buildItemGraph, catchUpText, DONE, endingPhrase, HIDDEN, keyOf, LABELS, num, pagelessPhrase, pageState,
  hasKey, own, pyFixed, relTime, ROW, s, statusOf, taskLabel, treeHref, usd, viewHref, WORKING, x,
  type GraphRow, type Item, type ItemContext as Ctx, type Surface,
} from '../model';

/** Lucide "archive", as statusd.ARCHIVE_ICON */
const ArchiveIcon = () => (
  <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect width="20" height="5" x="2" y="3" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" />
  </svg>
);

function Spent({ sf, ctx }: { sf: Surface; ctx: Ctx }) {
  const x = ctx.spent[s(sf.task)];
  return x ? <span className="badge cost" title="what this task's finished turns cost">{usd(x)}</span> : null;
}

function HistoryLink({ sf, ctx }: { sf: Surface; ctx: Ctx }) {
  const n = Math.trunc(num(ctx.histories[s(sf.artifact)]));
  return n ? <a className="hist" href={`/history/${keyOf(sf)}`}>{`history (${n})`}</a> : null;
}

/** _page_link: the earlier-rounds link and the ⋮ menu (Archive) */
function Menu({ sf, rounds = true }: { sf: Surface; rounds?: boolean }) {
  const key = keyOf(sf);
  const n = (sf.earlier || []).length;
  return (
    <>
      {n && rounds ? <a className="hist" href={`/page/${key}`}>{`${n} earlier round${n !== 1 ? 's' : ''}`}</a> : null}
      <details className="rowmenu">
        <summary className="hist" title="More" aria-label="More actions">⋮</summary>
        <div className="menu">
          <form method="post" action="/repair">
            <input type="hidden" name="key" value={key} />
            <input type="hidden" name="action" value="archive" />
            <input type="hidden" name="back" value="home" />
            <button type="submit"><ArchiveIcon />Archive</button>
          </form>
        </div>
      </details>
    </>
  );
}

interface PRowOpts {
  when?: boolean;
  head?: boolean;
  /** graph: the svg cell that replaces the kind label */
  cell?: ReactNode;
  /** graph: what follows the title link */
  extra?: ReactNode;
  grow?: boolean;
  done?: boolean;
}

/** _prow: one page of an item */
function PRow({ sf, ctx, o = {} }: { sf: Surface; ctx: Ctx; o?: PRowOpts }) {
  const [label, cls] = pageState(sf, ctx.statuses);
  const n = (sf.earlier || []).length;
  const k = n ? `round ${n + 1}` : s(sf.kind || 'page');
  const name = s(sf.title || sf.kind || 'page');
  const unseen = statusOf(sf, ctx.statuses).unseen === true;
  return (
    <div className={o.grow ? `page grow${o.done ? ' done' : ''}` : 'page'}>
      {o.cell ?? <span className="k">{k}</span>}
      <a className="t" href={viewHref(sf.path)} title={name}>{name}</a>
      {o.extra}
      {unseen && <span className="badge you">unopened</span>}
      {(cls === 'running' || cls === 'needs') && <span className={`badge ${cls === 'running' ? 'work' : 'q'}`}>{label}</span>}
      {o.when && <span className="badge" title="when this page was written">{relTime(num(sf.opened), ctx.now)}</span>}
      <Spent sf={sf} ctx={ctx} />
      {!o.head && <HistoryLink sf={sf} ctx={ctx} />}
      <Menu sf={sf} rounds={!o.head} />
    </div>
  );
}

/** _earlier: rows the driver rarely needs, one click away */
function Earlier({ rows, noun }: { rows: ReactNode[]; noun: string }) {
  if (!rows.length) return null;
  return (
    <details className="earlier">
      <summary>{`${rows.length} ${noun}${rows.length !== 1 ? 's' : ''}`}</summary>
      {rows}
    </details>
  );
}

function Cell({ r, width }: { r: GraphRow; width: number }) {
  return (
    <svg width={width} height={ROW} aria-hidden="true">
      {r.lines.map((d, i) => <path key={i} className="e" d={d} />)}
      <circle className={`n ${r.state}`} cx={x(r.depth)} cy={ROW / 2} r="4.5" />
    </svg>
  );
}

/** _graph: the item drawn as the tree that made it */
/**
 * Opens a graph row to show its history: the page's earlier rounds and any
 * settled branch folded under it. Not on the Python page; marked so the
 * parity harness leaves it out of the comparison.
 */
function HistoryToggle({ r, onToggle }: { r: GraphRow; onToggle: (task: string) => void }) {
  if (!r.history) return null;
  const what = `${r.history} earlier page${r.history !== 1 ? 's' : ''} on this branch`;
  return (
    <button type="button" className="hcaret" data-beyond-parity="" aria-expanded={r.open}
      title={r.open ? `hide the ${what}` : `show the ${what}`} onClick={() => onToggle(r.task)}>
      {r.open ? '▾' : '▸'}
    </button>
  );
}

/** An earlier round of the page above it, drawn as a graph row of its own. */
function RoundRow({ r, width, of, ctx }: { r: GraphRow; width: number; of: number; ctx: Ctx }) {
  const e = r.round!;
  return (
    <div className="page grow done" data-beyond-parity-row="">
      <Cell r={r} width={width} />
      <a className="t" href={viewHref(e.path)} title={e.title}>{e.title}</a>
      <span className="rn">{`round ${of}`}</span>
      <span className="badge" title="when this round was written">{relTime(e.opened, ctx.now)}</span>
    </div>
  );
}

function ItemGraph({ it, ctx }: { it: Item; ctx: Ctx }) {
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = useCallback((task: string) => setOpen((cur) => {
    const next = new Set(cur);
    if (!next.delete(task)) next.add(task);
    return next;
  }), []);
  const g = useMemo(() => buildItemGraph(it.key, it.pages, ctx.tree, ctx.kids, ctx.statuses, ctx.now, ctx.endings, ctx.bare, open),
    [it.key, it.pages, ctx, open]);
  const head = { when: true, head: true };
  // a round's number: the page above it is the newest, its earlier rounds count down
  const roundNo = (r: GraphRow) => {
    const above = g.rows.find((x) => x.page?.earlier?.some((e) => e.key === r.round!.key));
    const earlier = above?.page?.earlier || [];
    return earlier.length - earlier.findIndex((e) => e.key === r.round!.key);
  };
  return (
    <>
      {g.loose.map((sf) => <PRow key={keyOf(sf)} sf={sf} ctx={ctx} o={head} />)}
      <div className="graph">
        {g.rows.map((r) => {
          if (r.round) return <RoundRow key={r.task} r={r} width={g.width} of={roundNo(r)} ctx={ctx} />;
          const st = r.state;
          const more = r.hidden ? <span className="badge" title={`${r.hidden} more, all answered`}>{`+${r.hidden}`}</span> : null;
          if (r.page) {
            const sf = r.page;
            const n = (sf.earlier || []).length + 1;
            const [, cls] = pageState(sf, ctx.statuses);
            return (
              <PRow key={r.task} sf={sf} ctx={ctx} o={{
                ...head, grow: true, done: st === 'done', cell: <Cell r={r} width={g.width} />,
                extra: <>
                  <HistoryToggle r={r} onToggle={toggle} />
                  {n > 1 && <span className="rn" title={`${n} rounds`}>{`r${n}`}</span>}
                  {st === 'work' && cls !== 'running' && <span className="badge work">running</span>}
                  {more}
                </>,
              }} />
            );
          }
          const info = ctx.tree[r.task] || {};
          const title = s(info.title || taskLabel(r.task));
          const href = info.page ? viewHref(info.page) : treeHref(r.task);
          const said = st === 'work' ? <span className="badge work">running</span>
            : hasKey(ctx.endings, r.task) ? <span className="badge q">{endingPhrase(ctx.endings[r.task], ctx.now)}</span>
              : st === 'bad' ? <span className="badge q">{pagelessPhrase(ctx.bare[r.task], ctx.now)}</span>
                : <span className="badge">ended, no page</span>;
          return (
            <div key={r.task} className={`page grow${st === 'done' ? ' done' : ''}`}>
              <Cell r={r} width={g.width} />
              <a className="t" href={href} title={title}>{title}</a><HistoryToggle r={r} onToggle={toggle} />{said}{more}
            </div>
          );
        })}
      </div>
    </>
  );
}

function ItemButton({ item, action, label, tag }: { item: string; action: string; label: string; tag?: string }) {
  return (
    <form method="post" action="/items">
      <input type="hidden" name="item" value={item} />
      <input type="hidden" name="action" value={action} />
      {tag !== undefined && <input type="hidden" name="tag" value={tag} />}
      <button type="submit">{label}</button>
    </form>
  );
}

/** _item_actions: Catch me up, done, hide, tags and new work */
function Actions({ it, onNewWork }: { it: Item; onNewWork: (parent: string) => void }) {
  const key = it.key;
  const cmu = !key.startsWith('page:') && it.cwd && it.pages.length;
  return (
    <div className="acts">
      {cmu ? (
        <form className="cmu" method="post" action="/tasks">
          <input type="hidden" name="text" value={catchUpText(it)} />
          <input type="hidden" name="cwd" value={it.cwd} />
          <input type="hidden" name="parent" value={key} />
          <button type="submit" title="starts an agent that writes a short brief of what changed since you last opened this">
            Catch me up
          </button>
        </form>
      ) : null}
      {it.state === DONE ? <ItemButton item={key} action="undone" label="Reopen" />
        : <ItemButton item={key} action="done" label="Mark done" />}
      {it.state === HIDDEN ? <ItemButton item={key} action="unhide" label="Show on home" />
        : <ItemButton item={key} action="hide" label="Hide from home" />}
      <details className="addtag">
        <summary className="btn ghost">+ Tag</summary>
        <form method="post" action="/items">
          <input type="hidden" name="item" value={key} />
          <input type="hidden" name="action" value="tag" />
          <input name="tag" placeholder="tag, then Enter" aria-label="tag" size={12} maxLength={32} />
        </form>
      </details>
      {!key.startsWith('page:') && (
        <a className="btn ghost nwu" data-parent={key} href={`/?parent=${encodeURIComponent(key)}#newwork`}
          onClick={(e) => { e.preventDefault(); onNewWork(key); }}>New work under this</a>
      )}
      {it.tags.map((t) => <ItemButton key={t} item={key} action="untag" label={`#${t} ×`} tag={t} />)}
    </div>
  );
}

function stateBadge(it: Item) {
  if (it.state === WORKING) {
    const n = it.agents || 0;
    return <span className="badge work">{n ? `${n} agent${n !== 1 ? 's' : ''} running` : 'Agent working'}</span>;
  }
  const cls = { 'your-turn': 'you', idle: 'idle', done: 'done', hidden: 'idle' }[it.state];
  return <span className={`badge ${cls}`}>{LABELS[it.state]}</span>;
}

function whenTitle(it: Item, now: number): string {
  const touched = relTime(it.touched, now);
  return it.returned ? `newest result ${relTime(it.returned, now)}; touched ${touched}` : `no result yet; touched ${touched}`;
}

export interface ItemProps {
  it: Item;
  ctx: Ctx;
  offlist?: boolean;
  open: boolean;
  onToggle: (key: string, open: boolean) => void;
  onNewWork: (parent: string) => void;
  textRef?: (key: string, el: HTMLElement | null) => void;
  /** bumped when the list is redrawn the way the Python page redraws it */
  drawn?: number;
}

/**
 * One item: its summary row, then its pages (or its graph) and actions.
 * Memoized: layout() hands back the same item object when its state did not
 * move, so a keystroke in find redraws only the items it changes.
 */
export const ItemView = memo(function ItemView({ it, ctx, offlist, open, onToggle, onNewWork, textRef, drawn = 0 }: ItemProps) {
  const key = it.key;
  const hasKids = !!ctx.kids[key]?.length;
  const cur: ReactNode[] = [];
  const old: ReactNode[] = [];
  it.pages.forEach((sf, n) => {
    const row = <PRow key={keyOf(sf) || n} sf={sf} ctx={ctx} />;
    (n === 0 || !answered(sf, ctx.statuses) ? cur : old).push(row);
  });
  // An item with a tree draws it as its graph. (statusd._items_html also
  // builds an "Agents started from this" list, but only for an item with no
  // tree, where it is always empty, so it never shows; it is not drawn here.)
  const pages: ReactNode = hasKids ? <ItemGraph it={it} ctx={ctx} />
    : cur.length || old.length ? <>{cur}<Earlier rows={old} noun="answered page" /></>
      : <p className="empty">No page yet.</p>;
  const pname = own(ctx.names, it.project) ?? it.project;
  const stored = it.state === DONE || it.state === HIDDEN ? it.state : '';
  return (
    <details
      ref={(el) => textRef?.(key, el)}
      className={offlist ? 'item offlist' : 'item'} data-fold={`${ctx.fold}|${key}`} data-item={key}
      data-project={it.project} data-projname={pname} data-state={it.state} data-store={stored}
      data-working={it.working ? 1 : 0} data-agents={it.agents || 0} data-needs={it.needs}
      data-touched={Math.trunc(it.touched)} data-returned={Math.trunc(it.returned)} data-spent={pyFixed(it.spent, 2)}
      data-unopened={it.unopened ? 1 : 0} data-tags={it.tags.join(',')}
      open={open} onToggle={(e) => onToggle(key, (e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="row">
        <span className="caret"></span>
        {it.unopened && <span className="unseen" title="has a page you have not opened since the agent wrote it"></span>}
        <span className="title">{it.title}</span>
        <span className="stbadge">{stateBadge(it)}</span>
        {it.bare && <span className="badge q" title="a run in this item stopped without leaving a page to read">{it.bare}</span>}
        {it.tags.map((t) => <span key={t} className="tag">{`#${t}`}</span>)}
        <span className="proj">{pname}</span>
        {it.spent ? <span className="badge cost" title="every turn of every task in this item">{usd(it.spent)}</span> : null}
        <span className="when" title={whenTitle(it, ctx.now)}>{relTime(it.returned || it.touched, ctx.now)}</span>
      </summary>
      <div className="inside" key={drawn}>
        <div className="grp">{hasKids ? 'Pages, as the tree that made them' : 'Pages'}</div>
        <div className="pages">{pages}</div>
        <div className="threads" data-item={key}></div>
        <Actions it={it} onNewWork={onNewWork} />
      </div>
    </details>
  );
});

