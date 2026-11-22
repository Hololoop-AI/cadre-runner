// A cadre's app, at /c/<id>: the shell around whichever of its screens is
// open. The shell is the same for every cadre and comes from the cadre's
// navigation list (GET /api/nav?cadre=<id>, runnerlib/cadres.py nav):
//
//   wide   the cadres as a rail of neon icons down the left (Groups.tsx),
//          this cadre's pages as quiet icons beside it (PageRail, no column
//          behind them), and the same pages as named tabs under the header
//          (PageTabs). One fold button folds the rail and the icons away and
//          the browser remembers it; the tabs always stay;
//   phone  one menu button in the header holding the cadres as a strip, the
//          pages with their names, and the whole fleet's screens; the tabs
//          show under the header as on a wide screen.
//
// Moving to another tab or cadre happens in the app (router.goInApp): no
// reload, so the screen never blanks; where the browser can, the old screen
// crossfades into the new one (shell.css view transitions).
//
// Each screen is drawn inside the shell (Framed, Screen.tsx), so the rail,
// the tabs and Settings stay in reach from every one of them.

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { buildHome, cadreHref, groupBadges, HOLO, HOME_GROUP, OTHER, serverView, type Fleet, type Group, type Nav } from '../model';
import { RecordsScreen } from './Records';
import { BlackboardScreen } from './Blackboard';
import { AgentsList, ReviewScreen } from './Learn';
import { GroupBar } from './Groups';
import { PageIcon } from './PageIcon';
import { KanbanScreen } from './Kanban';
import { goInApp } from './router';
import { Framed } from './Screen';
import { useServer } from './server';
import './shell.css';
import { TopBar } from './TopBar';

// a cadre's path lives in the model, so screens that need only the path do not pull in the shell
export { cadreHref };

/** The rail entry a rail id opens: Home is Holo (/), any cadre its own app. */
export function railPick(id: string) {
  // inside the app, like the tabs: no reload, so no flash between cadres
  goInApp(cadreHref(id));
}

/** the browser's memory of the rails folded away */
const RAIL_SHUT = 'cadre.railShut';

/**
 * The cadre's pages as named tabs under the header, on every screen and
 * whether or not the rails are folded; the open one is underlined, and the
 * underline slides to the next tab as it opens (shell.css). A click moves in
 * the app (router.keepCadreMovesInApp): nothing reloads, so nothing flashes.
 */
function PageTabs({ nav, tab }: { nav: Nav; tab: string }) {
  return (
    <nav className="ptabs" aria-label={`${nav.cadre.name} pages`}>
      {nav.screens.map((s) => (
        <a key={s.href} href={s.href} className={s.id === tab ? 'on' : ''} aria-current={s.id === tab ? 'page' : undefined}
         >
          <span className="gi"><PageIcon id={s.id || ''} label={s.label} /></span>{s.label}
          {s.id === tab && <span className="tabline" aria-hidden="true" />}
        </a>
      ))}
    </nav>
  );
}

/** What a screen inside the shell draws its header with: the phone's menu button and the tabs. */
export interface Chrome {
  /** the cadre being shown */
  cadre: string;
  name: string;
  /** the phone's one menu button (hidden when wide) */
  menu: ReactNode;
  /** the phone's open menu (wide, the pages are in the rail) */
  tabs: ReactNode;
  /** the cadre's navigation, once it has arrived */
  nav: Nav | null;
}
const ChromeContext = createContext<Chrome | null>(null);

/** The chrome of the cadre the screen is in, or null outside one. */
export function useChrome(): Chrome | null {
  return useContext(ChromeContext);
}

/**
 * The cadre's pages as quiet neon icons beside the cadre rail (wide; they
 * fold away with it, and the tabs remain); on a phone, the same icons with
 * their names inside the menu. The open page glows. A click moves in the app
 * (router.keepCadreMovesInApp).
 */
export function PageRail({ nav, tab }: { nav: Nav; tab: string }) {
  return (
    <nav className="pages-rail" aria-label={`${nav.cadre.name} pages`}>
      {nav.screens.map((s) => (
        <a key={s.href} href={s.href} className={s.id === tab ? 'on' : ''} aria-current={s.id === tab ? 'page' : undefined} title={s.label}
         >
          <span className="gi"><PageIcon id={s.id || ''} label={s.label} /></span>
          <span className="gn">{s.label}</span>
        </a>
      ))}
    </nav>
  );
}

/** The header of a cadre screen that is not its home: the cadre's name, the menu button, the way back to Holo. */
export function CadreBar({ chrome, title }: { chrome: Chrome; title: string }) {
  useEffect(() => { document.title = `${chrome.name} — ${title}`; }, [chrome.name, title]);
  return (
    <>
      <TopBar>
        <h1>Cadre<span className="dot">.</span><span className="group">{chrome.name.toLowerCase().replace(/\s+/g, '-')}</span></h1>
        {chrome.menu}<span className="sp"></span><a href="/">Holo</a>
      </TopBar>
      {chrome.tabs}
    </>
  );
}

/** The fleet payload's rail, read once for the rail's badges and names. */
function useRail(fleet: Fleet | null): { groups: Group[]; badges: Record<string, number> } {
  return useMemo(() => {
    if (!fleet) return { groups: [], badges: {} };
    const home = buildHome(fleet, serverView(''), Date.now() / 1000, '');
    return { groups: home.groups, badges: groupBadges(home.items, home.member, home.groups) };
  }, [fleet]);
}

/**
 * The shell for cadre `id`, drawing `screen(chrome)` inside it. `fleet` is
 * the payload the rail's badges come from (null until it arrives).
 */
export function CadreShell({ id, tab, fleet, children }: {
  id: string; tab: string; fleet: Fleet | null; children: ReactNode;
}) {
  const server = useServer();
  // the navigation and its error are the cadre's they were fetched for: after a move to another
  // cadre, the old one's tabs and a stale error are never drawn under the new cadre
  const [got, setGot] = useState<{ id: string; nav: Nav | null; error: string }>({ id, nav: null, error: '' });
  const nav = got.id === id ? got.nav : null;
  const error = got.id === id ? got.error : '';
  useEffect(() => {
    // a move to another cadre in the app: only the newest cadre's navigation is drawn
    let stale = false;
    server.fetchNav(id === OTHER ? 'other' : id === HOLO ? 'holo' : id)
      .then((n) => { if (!stale) setGot({ id, nav: n, error: '' }); })
      .catch((e: unknown) => { if (!stale) setGot({ id, nav: null, error: e instanceof Error ? e.message : String(e) }); });
    return () => { stale = true; };
  }, [server, id]);
  const [open, setOpen] = useState(false); // the phone's menu
  // a move to another screen or cadre (in the app, no reload) closes the phone's menu
  useEffect(() => setOpen(false), [id, tab]);
  const [shut, setShut] = useState(() => window.localStorage?.getItem(RAIL_SHUT) === '1');
  const keepShut = (s: boolean) => {
    setShut(s);
    window.localStorage?.setItem(RAIL_SHUT, s ? '1' : '0');
  };
  const { groups, badges } = useRail(fleet);
  const name = nav?.cadre.name || (id === OTHER ? 'Other' : id);
  const menu = (
    <button type="button" className="cmenu" aria-expanded={open} aria-controls="cdrawer"
      aria-label={`Menu: cadres, ${name}'s screens and settings`} onClick={() => setOpen((o) => !o)}>
      {open ? '✕' : '☰'}
    </button>
  );
  const tabs = nav ? (
    open ? (
      <div id="cdrawer" className="cdrawer">
        <h3>{name}</h3><PageRail nav={nav} tab={tab} />
        <h3>The whole fleet</h3>
        <p className="cfleet"><a href="/">Holo</a><a href="/runs">every run</a><a href="/costs">costs</a><a href="/cards">cards</a><a href="/library">library</a></p>
      </div>
    ) : <PageTabs nav={nav} tab={tab} />
  ) : error ? <p className="notice">{`Could not load this cadre's navigation: ${error}`}</p> : null;
  return (
    <ChromeContext.Provider value={{ cadre: id, name, menu, tabs, nav }}>
      <GroupBar groups={groups} group={id === HOLO ? HOME_GROUP : id} badges={badges} shut={shut} open={open}
        onPick={(g) => { setOpen(false); railPick(g); }} onShut={keepShut} />
      {/* the pages as quiet icons beside the cadre rail; folded, they slide away and the tabs remain */}
      {nav && <div className={shut ? 'pages-wide gone' : 'pages-wide'} inert={shut || undefined}><PageRail nav={nav} tab={tab} /></div>}
      <div className="cmainwrap">{children}</div>
    </ChromeContext.Provider>
  );
}

/** A cadre screen that is not its home: the bar, then the screen drawn without its own frame. */
export function CadreScreen({ id, tab, fleet = null }: { id: string; tab: string; fleet?: Fleet | null }) {
  const chrome = useChrome();
  if (!chrome) return null;
  const title = chrome.nav?.screens.find((s) => s.id === tab)?.label || tab;
  return (
    <div className="wrap">
      <CadreBar chrome={chrome} title={title} />
      <Framed.Provider value>
        {tab === 'kanban' ? <KanbanScreen cadre={id} fleet={fleet} />
          : tab === 'blackboard' ? <BlackboardScreen />
            : tab === 'records' ? <RecordsScreen cadre={id === OTHER ? 'other' : id === HOLO ? 'holo' : id} name={chrome.name} />
            : tab === 'settings' ? (id === OTHER || id === HOLO ? <OtherSettings holo={id === HOLO} /> : <CadreSettingsScreen id={id} nav={chrome.nav} />)
              : tab === 'agents' ? (chrome.nav?.agents?.length ? <AgentsList name={chrome.name} agents={chrome.nav.agents} /> : <AgentsPending name={chrome.name} />)
                : tab === 'review' ? <ReviewScreen back={`/c/${encodeURIComponent(id)}/review`} />
                : <p className="notice">{`${chrome.name} has no screen called ${tab}.`}</p>}
      </Framed.Provider>
    </div>
  );
}

/**
 * A cadre's Settings tab: its own setup, not its work. What it is (its
 * project record), where its runs come from (its folders and the projects
 * grouped under it), its tabs and pinned pages, and how each is changed.
 * Its runs and the launcher are on Home and on the project page it links.
 */
function CadreSettingsScreen({ id, nav }: { id: string; nav: Nav | null }) {
  if (!nav) return <p className="meta">Loading…</p>;
  const st = nav.settings;
  const tabs = nav.screens.map((s) => s.label).join(', ');
  return (
    <section className="csettings">
      <h2 className="sect">Settings</h2>
      <p className="meta">{`How ${nav.cadre.name} is set up. Its work is on its Home tab; this page is only its setup.`}</p>
      <dl>
        <dt>Its record</dt>
        <dd>{`The project ${st.project || id}. `}<a href={`/project/${encodeURIComponent(st.project || id)}`}>Open the project page</a>
          {' for its launcher and context store, or to archive it. Its folders change with '}<code>{`pipeline.py project set ${st.project || id} --dir <folder>`}</code>{'; its tabs and pinned pages are the record\'s '}<code>nav</code>{' and '}<code>pinned</code>{' fields.'}</dd>
        <dt>Runs it gets</dt>
        <dd>
          {'Any run launched from inside it (New work here, or '}<code>{`pipeline.py task --cadre ${id}`}</code>{'). '}
          {'A run launched with no cadre named comes here when it runs in '}
          {st.folders.length || st.members.length ? 'one of these:' : 'its folder; it has none.'}
          {(st.folders.length > 0 || st.members.length > 0) && (
            <ul>
              {st.folders.map((f) => <li key={f}><code>{f}</code></li>)}
              {st.members.map((m) => <li key={m.id}>{'the project '}<a href={`/project/${encodeURIComponent(m.id)}`}>{m.name}</a></li>)}
            </ul>
          )}
          {'Move a run here with '}<code>{`pipeline.py cadres set <task> ${id}`}</code>{'.'}
        </dd>
        <dt>Tabs</dt>
        <dd>{`${tabs}${st.nav.length ? ', as its record asks (nav).' : ', the default.'}`}</dd>
        <dt>Pinned pages</dt>
        <dd>{nav.pinned.length ? nav.pinned.map((p) => p.label).join(', ') : 'None yet.'}</dd>
      </dl>
    </section>
  );
}

/** Other is not a project, so it has nothing of its own to set. */
function OtherSettings({ holo = false }: { holo?: boolean }) {
  if (holo) {
    return (
      <section>
        <h2 className="sect">Settings</h2>
        <p className="meta">Holo is the cadre that works on the cadres and the system themselves: its runs are the meta
          work (moving runs between cadres, reviving sessions, watching the fleet). It is not a project, so it has no record
          to set. A run is launched here with New work on <a href="/">Holo</a> or <code>pipeline.py task --cadre holo</code>,
          and moved here with <code>pipeline.py cadres set &lt;task&gt; holo</code>.</p>
      </section>
    );
  }
  return (
    <section>
      <h2 className="sect">Settings</h2>
      <p className="meta">Other holds the work that no cadre owns, so it has no settings of its own. Move a run into a cadre
        with <code>pipeline.py cadres set &lt;task&gt; &lt;cadre&gt;</code>; settings for the whole fleet live on <a href="/">Holo</a>.</p>
    </section>
  );
}

/** Agents (with their evals on each agent) is built in its own change; this tab is where it will sit. */
function AgentsPending({ name }: { name: string }) {
  return (
    <section>
      <h2 className="sect">Agents</h2>
      <p className="meta">{`${name}'s agents, each with its own evals, arrive with the agents view, which is being built in its own change. `}
        Until then, everything installed is in <a href="/library">the library</a>.</p>
    </section>
  );
}
