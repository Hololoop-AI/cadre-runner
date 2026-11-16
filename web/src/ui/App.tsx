// The fleet app. Every cadre is an app of its own with runs of its own
// (Cadre.tsx): `/c/<id>`, and `/` for Holo, the cadre that works on the
// cadres and the system, whose home also has a card per cadre. A cadre's
// home is its dashboard, drawn here: in the cadre that owns the kanban
// board, the board's cards lead, each holding the runs done for it, and the
// runs on no card follow as one-off work; Needs you and Running now sit
// beside them, all on one page. It holds only the cadre's own runs. The data is this machine's
// /api/fleet (useFleet); the model makes it into what is drawn (buildHome,
// layout).

import { useCallback, useEffect, useMemo, useState } from 'react';
import { formBody } from '../api';
import { buildHome, effectiveView, HOLO, inGroup, limitChoices, loadSel, needsYou, OTHER, runsOnCards, s, saveSel, serverView, tagChips, viewHref, workRows, type Kanban, type NewWorkInfo, type Sel } from '../model';
import { followThemeFavicon } from './favicon';
import { groupSuffix } from './Groups';
import { Header } from './Header';
import { CadreScreen, CadreShell, cadreHref, useChrome } from './Cadre';
import { CadreCardList, useHoloRedirect } from './Holo';
import { BacklogLine } from './CardWork';
import { OneWork, workSummary, type WorkMode } from './OneWork';
import { RunningNow } from './Running';
import { NewProjectForm, NewWorkDialog } from './NewWork';
import { Chips, Found, Toolbar } from './Toolbar';
import { useFind } from './useFind';
import { useFleet, type FleetState } from './useFleet';
import { CostsScreen } from './Costs';
import { KanbanScreen } from './Kanban';
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

/** Forms inside the Work list that post without leaving the app. */
const LIST_POSTS = ['/items', '/tasks', '/repair', '/kanban'];

/** Where the browser keeps cards or list. */
const WORK_MODE = 'cadre.workMode';

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
  // the Python pages give the body a class on home only; Holo and a cadre are homes
  useEffect(() => {
    document.body.className = here.screen === 'home' || here.screen === 'cadre' ? 'home cadre' : '';
  }, [here]);
  return (
    <ServerProvider base={machines.base}>
      {here.screen === 'home' ? <CadreApp key={machines.base} machines={machines} cadre={HOLO} tab="home" />
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
      {tab === 'home' ? <Home machines={machines} cadre={cadre} fleetState={fleet} /> : <CadreScreen id={cadre} tab={tab} fleet={fleet.fleet} />}
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
  // cards (a grid; one opens large) or list (a row each); the browser keeps the choice
  const [mode, setMode] = useState<WorkMode>(() => (window.localStorage?.getItem(WORK_MODE) === 'list' ? 'list' : 'cards'));
  const keepMode = (m: WorkMode) => { setMode(m); window.localStorage?.setItem(WORK_MODE, m); };
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
  const [work, setWork] = useState<{ open: boolean; parent: string | null }>({ open: !!query.get('parent'), parent: query.get('parent') });
  useHoloRedirect(cadre === HOLO);
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
  const nav = chrome?.nav;
  const hasKanban = !!nav?.screens.some((x) => x.id === 'kanban');
  useEffect(() => { document.title = `${chrome?.name || 'Cadre'} — home`; }, [chrome?.name]);
  // The cadre the board belongs to (its `board`) shows its active cards. The
  // fleet re-reads on every stream change, and a board change moves the
  // fleet's version, so the board is re-read with it; a read that lost the
  // race to a newer one, or to leaving the group, is dropped.
  const [board, setBoard] = useState<Kanban | null>(null);
  const picked = hasKanban;
  useEffect(() => {
    if (!picked) return setBoard(null);
    let stale = false;
    server.fetchKanban().then((b) => { if (!stale) setBoard(b); }).catch(() => { if (!stale) setBoard(null); });
    return () => { stale = true; };
  }, [server, fleet, picked]);
  // The cadre owning the board files its runs under the cards they were done
  // for; the Work list below holds the rest, the one-off runs.
  const ownsBoard = !!board && board.board === group;
  const onCards = useMemo(() => (ownsBoard ? runsOnCards(board) : new Set<string>()), [ownsBoard, board]);
  const loose = useMemo(() => (grouped && onCards.size ? (it: { key: string }) => grouped(it) && !onCards.has(it.key) : grouped),
    [grouped, onCards]);
  const laid = useWorkLayout(home, sel, { q, proj, sort, limit, grouped: loose });
  const all = useWorkLayout(home, sel, { q: '', proj: '', sort, limit, grouped });
  // Needs you: the cadre's own runs whose turn it is, whatever the chips and find show
  const waiting = all ? needsYou([...all.placed.list, ...all.placed.idle, ...all.placed.off], () => true) : [];
  // the list's sort and filters apply to the cards as to the runs on no card
  const workView = useMemo(() => ({ sort, proj, show: sel.show, unopened: sel.unopened }), [sort, proj, sel.show, sel.unopened]);
  const byKey = useMemo(() => new Map((home?.items || []).map((it) => [it.key, it])), [home]);
  const rowsAll = useMemo(() => (fleet && home && laid
    ? workRows(ownsBoard ? board : null, fleet, [...laid.placed.list, ...laid.placed.idle], now, byKey, workView) : null),
  [fleet, home, laid, ownsBoard, board, now, byKey, workView]);
  // the state chips count the cards too: a card is Your turn, Agent working or Idle by its runs
  const chipCount = useMemo(() => {
    const out: Record<string, number> = { ...(laid?.placed.count || {}) };
    if (!fleet || !ownsBoard || !board) return out;
    const cards = workRows(board, fleet, [], now, byKey, { proj }).rows;
    for (const r of cards) {
      const st = r.urgency === 0 ? 'your-turn' : r.urgency === 1 ? 'working' : 'idle';
      out[st] = (out[st] || 0) + 1;
      if (r.kind === 'card' && r.tally.fresh) out.unopened = (out.unopened || 0) + 1;
    }
    return out;
  }, [laid, fleet, ownsBoard, board, now, byKey, proj]);
  const found = useFind(q, home);
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
          groupName={group === HOLO ? 'holo' : groupSuffix(group, [...groups, { id: OTHER, name: 'Other' }])} menu={chrome?.menu} links={group === HOLO}
          boardHref={cadreHref(group, hasKanban ? 'kanban' : 'blackboard')} boardLabel={hasKanban ? 'board' : 'blackboard'} />
        {chrome?.tabs}
        {notice && <p className="notice">{notice}</p>}
        {error && <p className="notice">{`Could not load the fleet: ${error}`}</p>}
        {group === HOLO && fleet && home && <CadreCardList fleet={fleet} home={home} />}
        <div className="cgrid">
        <section className="cwork cmain" aria-label="Work">
          <h2 className="sect">{group === HOLO ? 'Holo\'s own work' : 'Work'}
            {rowsAll && <span className="wsum">{workSummary(rowsAll)}</span>}
            <span className="wmode" role="group" aria-label="Show the work as">
              {(['cards', 'list'] as const).map((m) => (
                <button key={m} type="button" aria-pressed={mode === m} onClick={() => keepMode(m)}>{m === 'cards' ? 'Cards' : 'List'}</button>
              ))}
            </span>
            {ownsBoard && <a href={cadreHref(group, 'kanban')}>the board →</a>}</h2>
          <Toolbar q={q} setQ={setQ} sort={sort} setSort={(v) => keep({ sort: v })} proj={proj}
            setProj={(v) => keep({ proj: v })} limit={limit} limits={limitChoices(view.limit, sel.limit)}
            setLimit={(n) => keep({ limit: n })} projects={projects} folded
            chips={laid && home && (
              <Chips part="states" sel={sel} count={chipCount} tags={[]}
                onState={(st) => (st === 'unopened' ? keep({ unopened: !sel.unopened })
                  : keep({ show: sel.show.includes(st) ? sel.show.filter((x) => x !== st) : [...sel.show, st] }))}
                onTag={() => {}} />
            )}
            more={laid && home && (
              <Chips part="tags" sel={sel} count={{}} tags={tagChips(home.tags, sel)}
                onState={() => {}} onTag={(t) => keep({ tag: sel.tag === t ? '' : t })} />
            )} />
          <Found q={q.trim()} hits={found?.hits ?? null} now={found?.now ?? now} />
          <div id="fleet" onSubmit={onListSubmit}>
            {fleet && home && laid && (
              <OneWork board={ownsBoard ? board : null} fleet={fleet} home={home} work={laid} view={workView} mode={mode} q={q}
                cadre={group === OTHER ? 'other' : group} folder={nav?.settings.folders[0] || ''} open={open}
                onToggle={onToggle} onNewWork={onNewWork} drawn={drawn + reloads}
                onChanged={(msg) => { setNotice(msg); refetch(); }} />
            )}
          </div>
          {ownsBoard && board && <BacklogLine board={board} kanbanHref={cadreHref(group, 'kanban')}
            onChanged={(msg) => { setNotice(msg); refetch(); }} />}
        </section>
        <aside className="cnext" aria-label="Needs you and running now">
          <section className="needs">
            <h2 className="sect">Needs you <span className="kn">{waiting.length}</span></h2>
            {!waiting.length ? <p className="meta">Nothing is waiting on you here.</p> : (
              <ul>{waiting.slice(0, NEEDS_SHOWN).map((it) => (
                <li key={it.key}><a href={it.head?.path ? viewHref(it.head.path) : `/tree/${encodeURIComponent(it.key)}`}>{it.title}</a></li>
              ))}</ul>
            )}
            {waiting.length > NEEDS_SHOWN && <p className="meta">{`and ${waiting.length - NEEDS_SHOWN} more in the Work list`}</p>}
          </section>
          <div id="fleet-running">
            {fleet && <RunningNow cards={(fleet.running || []).filter((c) => inGroup(home?.member[s(c.item || c.task)] || [], group))}
              slots={fleet.slots || 0} now={now} home={nw.home} />}
          </div>
        </aside>
        </div>
        {group === HOLO && fleet && <NewProjectForm key={reloads} projs={fleet.projs || {}} />}
      </div>
      <NewWorkDialog key={reloads} open={work.open} dataOpen={!!query.get('parent')} parent={work.parent}
        parents={home?.parents || []} projs={fleet?.projs || {}} home={nw.home} lib={nw.library}
        cadre={group === OTHER ? 'other' : group === HOLO ? 'holo' : group}
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
