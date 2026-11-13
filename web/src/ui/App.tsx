// The fleet home page, drawn to match statusd.render_home element for
// element: header, Running now, the Work toolbar and list, New project, the
// footer, and the New work dialog. The data is this machine's /api/fleet
// (useFleet); the model makes it into what is drawn (buildHome, layout).

import { useCallback, useEffect, useMemo, useState } from 'react';
import { formBody } from '../api';
import { buildHome, effectiveGroup, effectiveView, groupBadges, groupTag, inGroup, limitChoices, loadSel, saveSel, serverView, tagChips, type NewWorkInfo, type Sel } from '../model';
import { followThemeFavicon } from './GroupIcon';
import { GroupBar, GroupMenuButton, groupSuffix } from './Groups';
import { Header } from './Header';
import { NewProjectForm, NewWorkDialog } from './NewWork';
import { RunningNow } from './Running';
import { Chips, Found, Toolbar } from './Toolbar';
import { useFind } from './useFind';
import { useFleet } from './useFleet';
import { CostsScreen } from './Costs';
import { LibraryScreen } from './Library';
import { current, keepLinksInApp, postScreenForms } from './router';
import { PageScreen } from './Page';
import { ProjectScreen } from './Project';
import { RunsScreen } from './Runs';
import { TreeScreen } from './Tree';
import { MachineSwitch, ServerProvider, useMachines, useServer, type Machines } from './server';
import { useProjectOptions } from './useProjectOptions';
import { useWorkLayout } from './useWorkLayout';
import { WorkList } from './WorkList';

/** Forms inside the Work list that post without leaving the app. */
const LIST_POSTS = ['/items', '/tasks', '/repair'];

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
  // the Python pages give the body a class on home only
  useEffect(() => {
    document.body.className = here.screen === 'home' ? 'home' : '';
  }, [here]);
  return (
    <ServerProvider base={machines.base}>
      {here.screen === 'home' ? <Home key={machines.base} machines={machines} />
        : here.screen === 'library' ? <LibraryScreen key={machines.base} />
          : here.screen === 'costs' ? <CostsScreen key={machines.base} />
            : here.screen === 'runs' ? <RunsScreen key={machines.base} />
              : here.screen === 'project' ? <ProjectScreen key={machines.base} id={here.id} />
                : here.screen === 'page' ? <PageScreen key={machines.base} pageKey={here.key} />
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

function Home({ machines }: { machines: Machines }) {
  const server = useServer();
  const { fleet, now, live, error, refetch } = useFleet(server);
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
  const groups = home?.groups ?? [];
  const group = effectiveGroup(sel.group, groups);
  const [groupsOpen, setGroupsOpen] = useState(false); // a phone's groups, opened from the header
  const grouped = useMemo(() => (home && group ? (it: { key: string }) => inGroup(home.member[it.key] || [], group) : undefined),
    [home, group]);
  const laid = useWorkLayout(home, sel, { q, proj, sort, limit, grouped });
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
      {home && <GroupBar groups={groups} group={group} badges={groupBadges(home.items, home.member, groups)}
        shut={!!sel.groupsShut} open={groupsOpen} onPick={(id) => { keep({ group: id }); setGroupsOpen(false); }}
        onShut={(shut) => keep({ groupsShut: shut })} />}
      <div className="wrap">
        <Header weekUsd={fleet?.week_usd} live={live} snapshot={fleet?.snap?.iso}
          onNewWork={() => setWork({ open: true, parent: '' })} switcher={<MachineSwitch {...machines} />}
          cadre={groupSuffix(group, groups)}
          menu={<GroupMenuButton groups={groups} group={group} open={groupsOpen} onToggle={() => setGroupsOpen((o) => !o)} />} />
        {notice && <p className="notice">{notice}</p>}
        {error && <p className="notice">{`Could not load the fleet: ${error}`}</p>}
        <div id="fleet-running">
          {fleet && <RunningNow cards={fleet.running || []} slots={fleet.slots || 0} now={now} home={nw.home} />}
        </div>
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
        <footer>
          <a href="/library">installed skills, agents and workflows</a> · <a href="/tree">which session launched which</a> · <a href="/costs">what each run cost</a>
        </footer>
      </div>
      <NewWorkDialog key={reloads} open={work.open} dataOpen={!!query.get('parent')} parent={work.parent}
        parents={home?.parents || []} projs={fleet?.projs || {}} home={nw.home} lib={nw.library}
        tags={groupTag(group, groups)}
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
