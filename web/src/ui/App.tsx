// The fleet app. `/` is Hololoop: a card per cadre and no runs
// (Hololoop.tsx). `/c/<id>` is that cadre's own app (Cadre.tsx), whose home
// is its dashboard, drawn here: the board and the Work list lead in the main
// column, Needs you and Running now sit beside them, all on one page. Its
// list holds only the cadre's own runs. The data is this machine's
// /api/fleet (useFleet); the model makes it into what is drawn (buildHome,
// layout).

import { useCallback, useEffect, useMemo, useState } from 'react';
import { formBody } from '../api';
import { buildHome, effectiveView, inGroup, LABELS, limitChoices, loadSel, needsYou, OTHER, s, saveSel, serverView, tagChips, viewHref, type Kanban, type NewWorkInfo, type Sel } from '../model';
import { followThemeFavicon } from './favicon';
import { groupSuffix } from './Groups';
import { Header } from './Header';
import { CadreScreen, CadreShell, cadreHref, useChrome } from './Cadre';
import { Hololoop } from './Hololoop';
import { NewProjectForm, NewWorkDialog } from './NewWork';
import { RunningNow } from './Running';
import { Chips, Found, Toolbar } from './Toolbar';
import { useFind } from './useFind';
import { useFleet, type FleetState } from './useFleet';
import { CostsScreen } from './Costs';
import { ActiveCards, KanbanScreen, type CardWork } from './Kanban';
import { LibraryScreen } from './Library';
import { current, keepLinksInApp, postScreenForms } from './router';
import { PageScreen } from './Page';
import { ViewScreen } from './View';
import { ProjectScreen } from './Project';
import { RunsScreen } from './Runs';
import { TreeScreen } from './Tree';
import { MachineSwitch, ServerProvider, useMachines, useServer, type Machines } from './server';
import { useProjectOptions } from './useProjectOptions';
import { useWorkLayout } from './useWorkLayout';
import { WorkList } from './WorkList';

/** Forms inside the Work list that post without leaving the app. */
const LIST_POSTS = ['/items', '/tasks', '/repair', '/kanban'];

/** How many of Needs you the side column lists; the rest are in the Work list. */
const NEEDS_SHOWN = 8;

/** No groups yet: one array, so memos keyed on it hold while the fleet loads. */
const NO_GROUPS: never[] = [];

/**
 * The fleet app: the home page, or one of the other screens by path, for the
 * machine chosen in the header (this one until another is chosen).
 * Switching machines draws that machine's screen afresh.
 */
export function App() {
  const machines = useMachines();
  const [here] = useState(current);
  useEffect(keepLinksInApp, []);
  useEffect(() => followThemeFavicon(), []); // the tab icon in the theme's accent, as it changes
  // the Python pages give the body a class on home only; Hololoop and a cadre are homes
  useEffect(() => {
    document.body.className = here.screen === 'home' ? 'home' : here.screen === 'cadre' ? 'home cadre' : '';
  }, [here]);
  return (
    <ServerProvider base={machines.base}>
      {here.screen === 'home' ? <Hololoop key={machines.base} machines={machines} />
        : here.screen === 'cadre' ? <CadreApp key={machines.base} machines={machines} cadre={here.cadre} tab={here.tab} />
        : here.screen === 'library' ? <LibraryScreen key={machines.base} />
          : here.screen === 'costs' ? <CostsScreen key={machines.base} />
            : here.screen === 'runs' ? <RunsScreen key={machines.base} />
              : here.screen === 'kanban' ? <KanbanScreen key={machines.base} />
              : here.screen === 'project' ? <ProjectScreen key={machines.base} id={here.id} />
                : here.screen === 'page' ? <PageScreen key={machines.base} pageKey={here.key} />
                  : here.screen === 'view' ? <ViewScreen key={machines.base} pageKey={here.key} />
                  : <TreeScreen key={machines.base} task={here.task} />}
      <ScreenForms />
    </ServerProvider>
  );
}

/** Posts the screens' forms through the chosen machine's API (router.postScreenForms). */
function ScreenForms() {
  const server = useServer();
  useEffect(() => postScreenForms(server), [server]);
  return null;
}

/** A cadre's app: the shell, and inside it the screen its tab names. */
function CadreApp({ machines, cadre, tab }: { machines: Machines; cadre: string; tab: string }) {
  const server = useServer();
  const fleet = useFleet(server);
  return (
    <CadreShell id={cadre} tab={tab} fleet={fleet.fleet}>
      {tab === 'home' ? <Home machines={machines} cadre={cadre} fleetState={fleet} /> : <CadreScreen id={cadre} tab={tab} />}
    </CadreShell>
  );
}

/** A cadre's home: its dashboard, the board leading, Needs you and Running now beside it. */
function Home({ machines, cadre, fleetState }: { machines: Machines; cadre: string; fleetState: FleetState }) {
  const server = useServer();
  const chrome = useChrome();
  const { fleet, now, live, error, refetch } = fleetState;
  // The page's query, which the server draws from. It moves on a "reload".
  const [search, setSearch] = useState(location.search);
  const view = useMemo(() => serverView(search), [search]);
  const query = useMemo(() => new URLSearchParams(search), [search]);
  const [sel, setSel] = useState<Sel>(() => loadSel(location.search, window.localStorage));
  const keep = (patch: Partial<Sel>) => setSel((cur) => {
    const next = { ...cur, ...patch };
    saveSel(next, window.localStorage);
    return next;
  });
  const [q, setQ] = useState('');
  // Catch me up, Archive and Start are plain form posts on the Python page:
  // it reloads at /?notice=…, so open items close, find clears and every
  // form starts over. `reloads` counts those; `drawn` counts the lighter
  // redraw after an item action (menus close, the tag box clears) that the
  // Python page's re-fetch does.
  const [reloads, setReloads] = useState(0);
  const [drawn, setDrawn] = useState(0);
  const [notice, setNotice] = useState(query.get('notice') || '');
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  // Archived from this browser: hidden at once, before the re-fetch that
  // brings the server's own archived list (fleet.archived).
  const [archivedHere, setArchivedHere] = useState<Set<string>>(() => new Set());
  const [work, setWork] = useState({ open: !!query.get('parent'), parent: query.get('parent') });
  const [workError, setWorkError] = useState('');
  const [nw, setNw] = useState<NewWorkInfo>({ home: '', library: [] });
  useEffect(() => { server.fetchNewWork().then(setNw).catch(() => {}); }, [server]);

  const home = useMemo(() => (fleet ? buildHome(fleet, view, now, server.base || location.origin, archivedHere) : null),
    [fleet, view, now, archivedHere, server]);
  const projects = useProjectOptions(home, reloads);
  const { proj, sort, limit } = effectiveView(sel, view, projects);
  const groups = home?.groups ?? NO_GROUPS;
  const group = cadre; // a cadre's app holds only its own runs
  const grouped = useMemo(() => (home ? (it: { key: string }) => inGroup(home.member[it.key] || [], group) : undefined),
    [home, group]);
  useEffect(() => { document.title = `${chrome?.name || 'Cadre'} — home`; }, [chrome?.name]);
  // The cadre the board belongs to (its `board`) shows its active cards. The
  // fleet re-reads on every stream change, and a board change moves the
  // fleet's version, so the board is re-read with it; a read that lost the
  // race to a newer one, or to leaving the group, is dropped.
  const [board, setBoard] = useState<Kanban | null>(null);
  const picked = group !== OTHER;
  useEffect(() => {
    if (!picked) return setBoard(null);
    let stale = false;
    server.fetchKanban().then((b) => { if (!stale) setBoard(b); }).catch(() => { if (!stale) setBoard(null); });
    return () => { stale = true; };
  }, [server, fleet, picked]);
  const cardWork = useMemo<CardWork>(() => Object.fromEntries((home?.items || []).map((it) => [it.key, { title: it.title, state: LABELS[it.state] }])),
    [home]);
  const laid = useWorkLayout(home, sel, { q, proj, sort, limit, grouped });
  const found = useFind(q, home);
  // Needs you: the cadre's own items whose turn it is under the cadre's own
  // limit, whatever the chips and find show (layout() places every in-scope item)
  const waiting = laid ? needsYou([...laid.placed.list, ...laid.placed.idle, ...laid.placed.off], () => true) : [];

  const onToggle = useCallback((key: string, isOpen: boolean) => setOpen((cur) => {
    if (cur.has(key) === isOpen) return cur;
    const next = new Set(cur);
    if (isOpen) next.add(key);
    else next.delete(key);
    return next;
  }), []);
  const onNewWork = useCallback((parent: string) => setWork({ open: true, parent }), []);

  const reloaded = (msg: string) => {
    const next = `?${new URLSearchParams({ notice: msg })}`;
    // absolute: another machine's <base> would resolve a relative URL to its origin
    history.replaceState(null, '', location.origin + location.pathname + next);
    setSearch(next);
    setNotice(msg);
    setOpen(new Set());
    setQ('');
    setWork({ open: false, parent: null });
    setReloads((n) => n + 1);
    window.scrollTo(0, 0);
    refetch();
  };
  const onListSubmit = (e: React.FormEvent<HTMLDivElement>) => {
    const fm = e.target;
    if (!(fm instanceof HTMLFormElement)) return;
    const action = fm.getAttribute('action') || '';
    if (!LIST_POSTS.includes(action)) return;
    e.preventDefault();
    const data = formBody(fm);
    server.post(action, data).then(({ notice: msg, path, location: there }) => {
      // Add to board lands on the Board; the driver stays here and reads what happened
      if (action === '/kanban') return setNotice(msg);
      // landing anywhere but home (a refused Archive goes to the page's manage
      // page) is a navigation on the Python page too
      if (path !== '/') return window.location.assign(server.base + there);
      if (action === '/repair' && data.get('action') === 'archive') setArchivedHere((cur) => new Set([...cur, data.get('key') || '']));
      if (action !== '/items') return reloaded(msg);
      setDrawn((n) => n + 1);
      refetch();
    }).catch((err: unknown) => setNotice(err instanceof Error ? err.message : String(err)));
  };

  return (
    <>
      <div className="wrap cdash">
        <Header weekUsd={fleet ? Number(fleet.cadre_week_usd?.[group]) || 0 : undefined} live={live} snapshot={fleet?.snap?.iso}
          onNewWork={() => setWork({ open: true, parent: '' })} switcher={<MachineSwitch {...machines} />}
          groupName={groupSuffix(group, [...groups, { id: OTHER, name: 'Other' }])} menu={chrome?.menu}
          boardHref={cadreHref(group, 'kanban')} />
        {chrome?.tabs}
        {notice && <p className="notice">{notice}</p>}
        {error && <p className="notice">{`Could not load the fleet: ${error}`}</p>}
        <div className="cgrid">
        <div className="cmain">
        {board && board.board === group && <ActiveCards board={board} work={cardWork} href={cadreHref(group, 'kanban')} />}
        <h2 className="sect">Work</h2>
        <Toolbar q={q} setQ={setQ} sort={sort} setSort={(v) => keep({ sort: v })} proj={proj}
          setProj={(v) => keep({ proj: v })} limit={limit} limits={limitChoices(view.limit, sel.limit)}
          setLimit={(n) => keep({ limit: n })} projects={projects} />
        {laid && home && (
          <Chips sel={sel} count={laid.placed.count} tags={tagChips(home.tags, sel)}
            onState={(st) => (st === 'unopened' ? keep({ unopened: !sel.unopened })
              : keep({ show: sel.show.includes(st) ? sel.show.filter((x) => x !== st) : [...sel.show, st] }))}
            onTag={(t) => keep({ tag: sel.tag === t ? '' : t })} />
        )}
        <Found q={q.trim()} hits={found?.hits ?? null} now={found?.now ?? now} />
        <div id="fleet" onSubmit={onListSubmit}>
          {fleet && home && laid && (
            <WorkList home={home} fleet={fleet} work={laid} sel={sel} q={q} serverLimit={view.limit} open={open}
              onToggle={onToggle} onNewWork={onNewWork} onIdleOpen={(o) => keep({ idleOpen: o })}
              drawn={drawn + reloads} />
          )}
        </div>
        {fleet && <NewProjectForm key={reloads} projs={fleet.projs || {}} />}
        </div>
        <aside className="cnext" aria-label="Needs you and running now">
          <section className="needs">
            <h2 className="sect">Needs you <span className="kn">{waiting.length}</span></h2>
            {!waiting.length ? <p className="meta">Nothing is waiting on you here.</p> : (
              <ul>{waiting.slice(0, NEEDS_SHOWN).map((it) => (
                <li key={it.key}><a href={it.head?.path ? viewHref(it.head.path) : `/tree/${encodeURIComponent(it.key)}`}>{it.title}</a></li>
              ))}</ul>
            )}
            {waiting.length > NEEDS_SHOWN && <p className="meta">{`and ${waiting.length - NEEDS_SHOWN} more under Your turn in Work`}</p>}
          </section>
          <div id="fleet-running">
            {fleet && <RunningNow cards={(fleet.running || []).filter((c) => inGroup(home?.member[s(c.item || c.task)] || [], group))}
              slots={fleet.slots || 0} now={now} home={nw.home} />}
          </div>
        </aside>
        </div>
        <footer>
          <a href="/library">installed skills, agents and workflows</a> · <a href="/tree">which session launched which</a> · <a href="/costs">what each run cost</a>
        </footer>
      </div>
      <NewWorkDialog key={reloads} open={work.open} dataOpen={!!query.get('parent')} parent={work.parent}
        parents={home?.parents || []} projs={fleet?.projs || {}} home={nw.home} lib={nw.library}
        cadre={group === OTHER ? 'other' : group}
        onClose={() => setWork((w) => ({ ...w, open: false }))}
        error={workError}
        onSubmit={(fm) => {
          setWorkError('');
          // a rejected task is an answer (the Python page reloads with it); a
          // post that failed outright keeps the dialog and the typed text
          server.post('/tasks', fm).then(({ notice: msg }) => reloaded(msg || 'Task submitted.'))
            .catch((err: unknown) => setWorkError(err instanceof Error ? err.message : String(err)));
        }} />
    </>
  );
}
