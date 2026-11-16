// A cadre's app, at /c/<id>: the shell around whichever of its screens is
// open. The shell is the same for every cadre and comes from the cadre's
// navigation list (GET /api/nav?cadre=<id>, runnerlib/cadres.py nav):
//
//   wide   the cadre rail of neon icons down the left (Groups.tsx), the
//          cadre's screens as tabs under the header (always visible while
//          you are in a cadre), and a second column of batches and pinned
//          pages one click away, which folds;
//   phone  one menu button in the header holding all of it: the rail, the
//          screens (Settings among them) and the second column.
//
// Each screen is drawn inside the shell (Framed, Screen.tsx), so the rail,
// the tabs and Settings stay in reach from every one of them.

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { buildHome, groupBadges, HOLO, HOME_GROUP, OTHER, serverView, type Fleet, type Group, type Nav } from '../model';
import { RecordsScreen } from './Records';
import { BlackboardScreen } from './Blackboard';
import { GroupBar } from './Groups';
import { KanbanScreen } from './Kanban';
import { go } from './router';
import { Framed } from './Screen';
import { useServer } from './server';
import './shell.css';

/** A cadre's path: Other is /c/other, any other cadre /c/<its id>. */
export function cadreHref(id: string, tab = ''): string {
  if ((id === HOME_GROUP || id === HOLO) && (!tab || tab === 'home')) return '/';
  const base = `/c/${id === OTHER ? 'other' : id === HOLO || id === HOME_GROUP ? 'holo' : encodeURIComponent(id)}`;
  return tab && tab !== 'home' ? `${base}/${tab}` : base;
}

/** The rail entry a rail id opens: Home is Holo (/), any cadre its own app. */
export function railPick(id: string) {
  go(cadreHref(id));
}

const SIDE_SHUT = 'cadre.sideShut';

/** What a screen inside the shell draws its header with: the phone's menu button and the tabs. */
export interface Chrome {
  /** the cadre being shown */
  cadre: string;
  name: string;
  /** the phone's one menu button (hidden when wide) */
  menu: ReactNode;
  /** the header tabs when wide, and the phone's open menu */
  tabs: ReactNode;
  /** the cadre's navigation, once it has arrived */
  nav: Nav | null;
}
const ChromeContext = createContext<Chrome | null>(null);

/** The chrome of the cadre the screen is in, or null outside one. */
export function useChrome(): Chrome | null {
  return useContext(ChromeContext);
}

/** The cadre's screens as tabs, the current one marked. */
export function CadreTabs({ nav, tab }: { nav: Nav; tab: string }) {
  return (
    <nav className="ctabs" aria-label={`${nav.cadre.name} screens`}>
      {nav.screens.map((s) => (
        <a key={s.href} href={s.href} className={s.id === tab ? 'on' : ''} aria-current={s.id === tab ? 'page' : undefined}>{s.label}</a>
      ))}
    </nav>
  );
}

/** The second column: the cadre's batches and pinned pages, with Settings at its foot. */
function SideLists({ nav }: { nav: Nav }) {
  const settings = nav.screens.find((s) => s.id === 'settings');
  return (
    <>
      <h3>Batches</h3>
      {nav.batches.length ? <ul>{nav.batches.map((b) => <li key={b.href}><a href={b.href}>{b.label}</a></li>)}</ul>
        : <p className="meta">None yet: batches are made on the board in the next slice.</p>}
      <h3>Pinned</h3>
      {nav.pinned.length ? <ul>{nav.pinned.map((p) => <li key={p.href}><a href={p.href}>{p.label}</a></li>)}</ul>
        : <p className="meta">Nothing pinned. A page is pinned from the cadre's settings (the project record's <code>pinned</code>).</p>}
      {settings && <p className="cset"><a href={settings.href}>⚙ {nav.cadre.name} settings</a></p>}
    </>
  );
}

/** The second column when wide: one click from anywhere in the cadre, and it folds to a sliver. */
function SideColumn({ nav, shut, onShut }: { nav: Nav; shut: boolean; onShut: (s: boolean) => void }) {
  return (
    <aside className={shut ? 'cside shut' : 'cside'} aria-label={`${nav.cadre.name}: batches and pinned pages`}>
      <button type="button" className="cfold" aria-expanded={!shut} title={shut ? 'Show batches and pinned pages' : 'Fold this column away'}
        onClick={() => onShut(!shut)}>{shut ? '›' : '‹'}</button>
      {!shut && <div className="clists"><h2>{nav.cadre.name}</h2><SideLists nav={nav} /></div>}
    </aside>
  );
}

/** The header of a cadre screen that is not its home: the cadre's name, the menu button, the way back to Holo. */
export function CadreBar({ chrome, title }: { chrome: Chrome; title: string }) {
  useEffect(() => { document.title = `${chrome.name} — ${title}`; }, [chrome.name, title]);
  return (
    <>
      <header>
        <h1>Cadre<span className="dot">.</span><span className="group">{chrome.name.toLowerCase().replace(/\s+/g, '-')}</span></h1>
        {chrome.menu}<span className="sp"></span><a href="/">Holo</a>
      </header>
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
  const [nav, setNav] = useState<Nav | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    server.fetchNav(id === OTHER ? 'other' : id === HOLO ? 'holo' : id).then(setNav).catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [server, id]);
  const [open, setOpen] = useState(false); // the phone's menu
  const [shut, setShut] = useState(() => window.localStorage?.getItem(SIDE_SHUT) === '1');
  const keepShut = (s: boolean) => {
    setShut(s);
    window.localStorage?.setItem(SIDE_SHUT, s ? '1' : '0');
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
    <>
      <CadreTabs nav={nav} tab={tab} />
      {open && (
        <div id="cdrawer" className="cdrawer">
          <h3>{name}</h3><CadreTabs nav={nav} tab={tab} /><SideLists nav={nav} />
          <h3>The whole fleet</h3>
          <p className="cfleet"><a href="/">Holo</a><a href="/runs">every run</a><a href="/costs">costs</a><a href="/cards">cards</a><a href="/library">library</a></p>
        </div>
      )}
    </>
  ) : error ? <p className="notice">{`Could not load this cadre's navigation: ${error}`}</p> : null;
  return (
    <ChromeContext.Provider value={{ cadre: id, name, menu, tabs, nav }}>
      <GroupBar groups={groups} group={id === HOLO ? HOME_GROUP : id} badges={badges} shut={false} open={open}
        onPick={(g) => { setOpen(false); railPick(g); }} onShut={() => {}} />
      <div className={shut ? 'cshell shut' : 'cshell'}>
        {nav && <SideColumn nav={nav} shut={shut} onShut={keepShut} />}
        <div className="cmainwrap">{children}</div>
      </div>
    </ChromeContext.Provider>
  );
}

/** A cadre screen that is not its home: the bar, then the screen drawn without its own frame. */
export function CadreScreen({ id, tab }: { id: string; tab: string }) {
  const chrome = useChrome();
  if (!chrome) return null;
  const title = chrome.nav?.screens.find((s) => s.id === tab)?.label || tab;
  return (
    <div className="wrap">
      <CadreBar chrome={chrome} title={title} />
      <Framed.Provider value>
        {tab === 'kanban' ? <KanbanScreen cadre={id} />
          : tab === 'blackboard' ? <BlackboardScreen />
            : tab === 'records' ? <RecordsScreen cadre={id === OTHER ? 'other' : id === HOLO ? 'holo' : id} name={chrome.name} />
            : tab === 'settings' ? (id === OTHER || id === HOLO ? <OtherSettings holo={id === HOLO} /> : <CadreSettingsScreen id={id} nav={chrome.nav} />)
              : tab === 'agents' ? <AgentsPending name={chrome.name} />
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
