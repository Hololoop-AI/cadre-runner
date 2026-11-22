// The notifications on every fleet screen: what needs you on review pages (a
// turn that came back, a run that failed, an agent's reply), and quietly what
// agents are working on, read from Cadre's notification center
// (GET /api/notifications, runnerlib/notifications.py), with a soft chime when
// something new needs you.
//
// Two parts. `NotificationCenter` sits once at the app's root, so it runs on every
// screen, the page frame included (statusd sends every review page there): it
// re-reads the list when the fleet stream says something changed (the stream
// this tab already holds; no poll, no connection of its own) and chimes.
// `Bell` is the button and its menu, drawn by `TopBar` (the one header every
// screen uses) and by the frame's bar; the page server draws none of its own.
//
// Read state lives in this origin's localStorage (bellStore.ts).

import { createContext, useCallback, useContext, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from 'react';
import { bellStep, isoEpoch, isUnread, relTime, unreadNotifications, viewHref, type Notification } from '../model';
import { useServer } from './server';
import { playChime, unlockChimeOnGesture } from './chime';
import { CHIMED_KEY, read, readChimed, SEEN_KEY, SOUND_KEY, underChimeLock, write } from './bellStore';
import './bell.css';

interface BellState {
  /** the menu's rows: every page that moved but the one the frame shows */
  rows: Notification[];
  seen: string | null;
  sound: boolean;
  /** inside the page frame: the list leaves that page out */
  framed: boolean;
  /** where a row links: this machine's frame around the page */
  href: (row: Notification) => string;
  markSeen: () => void;
  setSound: (on: boolean) => void;
}
const BellContext = createContext<BellState | null>(null);

/**
 * The notification center's browser side, once at the app's root: fetches on
 * every fleet change, chimes, keeps read state. `exclude` is the page the
 * frame shows: the menu leaves it out (the page shows its own new version),
 * but its news still chimes, so waiting on an agent in its page rings too.
 */
export function NotificationCenter({ exclude = '', children }: { exclude?: string; children: ReactNode }) {
  const server = useServer();
  // another machine's rows are its own log: keep its read state apart
  const suffix = server.base ? ':' + server.base : '';
  const seenKey = SEEN_KEY + suffix;
  const chimedKey = CHIMED_KEY + suffix;
  const [rows, setRows] = useState<Notification[]>([]);
  const [seen, setSeen] = useState(() => read(seenKey));
  const [sound, setSoundState] = useState(() => read(SOUND_KEY) !== 'off');
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const asked = ++latest.current;
    let list: Notification[];
    try {
      list = (await server.fetchNotifications()).notifications || [];
    } catch {
      return; // no backend, or another machine refusing: no bell news
    }
    if (asked !== latest.current) return; // a newer fetch is on its way
    await underChimeLock(suffix, () => {
      const prevSeen = read(seenKey);
      const prevChimed = readChimed(chimedKey);
      const step = bellStep(list, prevChimed, prevSeen);
      if (step.seenAt !== prevSeen) write(seenKey, step.seenAt);
      if (!step.ring) {
        if (step.chimed !== prevChimed) write(chimedKey, JSON.stringify(step.chimed));
        return;
      }
      // claim the chime only when it played (or is muted): a tab that cannot play leaves it to one that can
      if (read(SOUND_KEY) === 'off' || playChime(step.ring)) write(chimedKey, JSON.stringify(step.chimed));
    });
    setSeen(read(seenKey));
    setRows(list);
  }, [server, suffix, seenKey, chimedKey]);

  useEffect(() => unlockChimeOnGesture(), []);
  useEffect(() => {
    void refresh();
    const stop = server.subscribe(() => void refresh());
    // this bell in another tab may have been opened
    const stored = (e: StorageEvent) => {
      if (e.key === seenKey) setSeen(e.newValue);
      if (e.key === SOUND_KEY) setSoundState(e.newValue !== 'off');
    };
    window.addEventListener('storage', stored);
    return () => {
      latest.current++; // answers still on their way are for a center that is gone
      stop();
      window.removeEventListener('storage', stored);
    };
  }, [server, refresh, seenKey]);

  const markSeen = () => {
    const newest = rows[0]?.at;
    // never backwards: another tab may have seen further
    if (!newest || newest <= (read(seenKey) || '')) return;
    write(seenKey, newest);
    setSeen(newest);
  };
  const shown = exclude ? rows.filter((row) => row.key !== exclude) : rows;
  const setSound = (on: boolean) => {
    write(SOUND_KEY, on ? 'on' : 'off');
    setSoundState(on);
    if (on) playChime();
  };
  const href = (row: Notification) => server.base + viewHref(row.url);
  return <BellContext.Provider value={{ rows: shown, seen, sound, framed: !!exclude, href, markSeen, setSound }}>{children}</BellContext.Provider>;
}

/** A 24-unit line icon, stroke 1.7, as the page server's chrome draws its own icons. */
function Icon({ size = 16, children }: { size?: number; children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
  );
}
/** The bell, path for path the one the page server's removed bell used, so it looks as it always did. */
const BELL = <Icon><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" /></Icon>;
const SOUND_ON = <Icon size={14}><path d="M11 5 6 9H2v6h4l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M19 5a10 10 0 0 1 0 14" /></Icon>;
/** A warning triangle, for a run that failed. */
const PROBLEM = <Icon size={13}><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4" /><path d="M12 17h.01" /></Icon>;
const SOUND_OFF = <Icon size={14}><path d="M11 5 6 9H2v6h4l5 4V5z" /><path d="m23 9-6 6" /><path d="m17 9 6 6" /></Icon>;

/** What the bell's badge shows: the unread count, whether one is a failed run, and the button's name. */
export function useBellBadge(): { count: number; problem: boolean; label: string } {
  const bells = useContext(BellContext);
  const unread = bells ? unreadNotifications(bells.rows, bells.seen) : [];
  const problems = unread.filter((row) => row.problem).length;
  const label = !unread.length ? 'Notifications' : `Notifications, ${unread.length} new` + (problems ? `, ${problems} failed` : '');
  return { count: unread.length, problem: problems > 0, label };
}

/** Where the menu hangs when something other than this bell's button opens it, in this window. */
export interface BellAnchor { top: number; right: number }
/** Opening and closing the bell from outside it: the framed page's own bell (PageBell.ts). */
export interface BellHandle { toggle: (at: BellAnchor) => void; close: () => void }

/**
 * The bell button and its menu, at the right end of the header (TopBar). Nothing outside
 * `NotificationCenter`. `button={false}` draws the menu only: something else is the button
 * and opens it through `handle`, saying where the menu hangs; `onOpenChange` tells it.
 */
export function Bell({ button = true, handle, onOpenChange }:
  { button?: boolean; handle?: Ref<BellHandle>; onOpenChange?: (open: boolean) => void } = {}) {
  const bells = useContext(BellContext);
  const badge = useBellBadge();
  const [open, setOpenState] = useState(false);
  const [at, setAt] = useState<BellAnchor | null>(null);
  const setOpen = (next: boolean) => {
    setOpenState(next);
    if (!next) setAt(null);
  };
  useEffect(() => { onOpenChange?.(open); }, [open, onOpenChange]);
  useEffect(() => {
    if (!at) return;
    const moved = () => setOpen(false);
    window.addEventListener('resize', moved);
    return () => window.removeEventListener('resize', moved);
  }, [at]);
  // what was seen before the menu opened: its rows keep their unread mark while it is open
  const [seenBefore, setSeenBefore] = useState<string | null>(null);
  const wrap = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    // a click in the page the frame shows lands in that page's document, never here;
    // what this window sees is losing focus to the iframe
    const intoPage = () => { if (document.activeElement instanceof HTMLIFrameElement) setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    window.addEventListener('blur', intoPage);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
      window.removeEventListener('blur', intoPage);
    };
  }, [open]);
  const toggle = (where: BellAnchor | null = null) => {
    if (!bells) return;
    if (!open) {
      setSeenBefore(bells.seen);
      bells.markSeen();
      setAt(where);
    }
    setOpen(!open);
  };
  useImperativeHandle(handle, () => ({ toggle, close: () => setOpen(false) }));
  if (!bells) return null;
  const { rows, seen, sound, framed, href, setSound } = bells;
  // opening clears the badge; the rows stay marked until the menu closes
  const rowSeen = open ? seenBefore : seen;
  return (
    <span className="fbell" ref={wrap} data-beyond-parity="">
      <button type="button" className={badge.count && badge.problem ? 'fbell-button has-unread has-problem' : badge.count ? 'fbell-button has-unread' : 'fbell-button'} title="Notifications"
        aria-haspopup="menu" aria-expanded={open} onClick={() => toggle()} hidden={!button}
        aria-label={badge.label}>
        {BELL}{badge.count > 0 && <span className="fbell-count">{badge.count}</span>}
      </button>
      {/* in the frame the page below is another document: the click that closes the menu
          lands on this instead, so it never reaches the page (no annotation from it),
          as the page's own menus swallow theirs */}
      {open && framed && <div className="fbell-shield" aria-hidden="true" onMouseDown={() => setOpen(false)} />}
      {open && (
        <div className={at ? 'fbell-menu at-page' : 'fbell-menu'} role="menu" aria-label="Notifications"
          style={at ? { position: 'fixed', top: at.top, right: at.right } : undefined}>
          <div className="fbell-head"><span>{framed ? 'On other pages' : 'On review pages'}</span>
            <button type="button" className={sound ? 'fbell-sound' : 'fbell-sound off'} aria-pressed={sound}
              aria-label={sound ? 'Sound on' : 'Sound off'} title={sound ? 'Sound on: a soft chime when something new arrives. Click to mute' : 'Sound off. Click to hear a chime when something new arrives'}
              onClick={() => setSound(!sound)}>{sound ? SOUND_ON : SOUND_OFF}</button>
          </div>
          {rows.length === 0 ? <p className="fbell-empty">{framed ? 'Nothing has moved on other pages yet.' : 'Nothing has moved on review pages yet.'}</p>
            : rows.map((row) => (
              <a key={row.key} role="menuitem" href={href(row)}
                className={'fbell-row' + (!row.actionable ? ' quiet' : isUnread(row, rowSeen) ? ' unread' : '') + (row.problem ? ' problem' : '')}
                onClick={() => setOpen(false)}>
                <span className="fbell-title">{row.problem && <span className="fbell-alert" aria-label="failed">{PROBLEM}</span>}{row.title || row.key}</span>
                <span className="fbell-meta">{`${row.label} · ${relTime(isoEpoch(row.at), Date.now() / 1000)}`}</span>
              </a>
            ))}
        </div>
      )}
    </span>
  );
}
