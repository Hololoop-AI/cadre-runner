// A project's running section, as statusd.render_fleet draws it for the
// project page (its older per-project shape, not the home page's items):
// conversations grouped by the project they file under, orchestrators as a
// byline, settled ones folded, and the badges each row carries.

import { keyOf, statusOf } from './conversations';
import { cmp, hasKey, isoEpoch, num, s, shortError } from './format';
import { agentStateBadge, unowned, STARTUP_GRACE } from './items';
import type { InstalledEntry, LibraryEntry, Pageless, Project, SessionStatus, Surface } from './payload';
import { liveProjects, namesOf } from './projects';

/** statusd.external_projects: open conversations grouped by project, busiest first. */
export function externalProjects(rows: Surface[]): { project: string; rows: Surface[] }[] {
  const groups = new Map<string, Surface[]>();
  for (const sf of rows) {
    if (!sf.project) continue;
    const k = String(sf.project);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(sf);
  }
  const out = [...groups].map(([project, list]) => {
    list.sort((a, b) => (a.role === 'orchestrator' ? 0 : 1) - (b.role === 'orchestrator' ? 0 : 1) || num(b.opened) - num(a.opened));
    return { project, rows: list, activity: Math.max(...list.map((r) => num(r.opened))) };
  });
  out.sort((a, b) => b.activity - a.activity);
  return out;
}

/** statusd._group_rows: orchestrators (the byline), live rows, and settled (DECIDED) rows. */
export function groupRows(src: Surface[]): { byline: Surface[]; rows: Surface[]; decided: Surface[] } {
  const byline = src.filter((sf) => sf.role === 'orchestrator' && !sf.path);
  const rows: Surface[] = [];
  const decided: Surface[] = [];
  for (const sf of src) {
    if (byline.includes(sf)) continue;
    const settled = s(sf.title).includes('DECIDED') && !(sf.stranded && sf.stranded.length);
    (settled ? decided : rows).push(sf);
  }
  return { byline, rows, decided };
}

/** One project's part of the section: the project or a member, and its rows. */
export interface SectionPart {
  project: Project;
  byline: Surface[];
  rows: Surface[];
}

/**
 * render_fleet's durable section for project `top` drawn from `mine` (the
 * project, ungrouped and live, and its members): its parts, how many rows
 * run and how many settled, and the groups no part claimed.
 */
export function projectSection(top: Project, mine: Record<string, Project>, rows: Surface[]) {
  const projs: Record<string, Project> = { ...mine, [top.id]: { ...top, group: null, archived: false } };
  const exts = externalProjects(rows);
  const claimed = new Set<string>();
  const parts: SectionPart[] = [];
  let live = 0;
  let done = 0;
  const members = liveProjects(projs).filter((m) => m.group === top.id);
  for (const m of [projs[top.id], ...members]) {
    const names = namesOf(projs, m.id);
    names.forEach((n) => claimed.add(n));
    const g = groupRows(exts.filter((e) => names.includes(e.project)).flatMap((e) => e.rows));
    live += g.rows.length;
    done += g.decided.length;
    parts.push({ project: m, byline: g.byline, rows: g.rows });
  }
  return { parts, live, done, unclaimed: exts.filter((e) => !claimed.has(e.project)) };
}

/** statusd._live's state badge: what the agent on a page is doing, from the driver's side. */
export function liveBadge(sf: Surface, statuses: Record<string, SessionStatus>, doing: Record<string, string>): [string, string] {
  const owned = sf.kind == null || sf.kind === 'page' || !sf.artifact ? null : !unowned(sf);
  let [label, cls] = agentStateBadge(statuses[keyOf(sf)], owned);
  const said = hasKey(doing, s(sf.task)) ? doing[s(sf.task)] : '';
  if (said && cls === 'running') label = said;
  return [label, cls];
}

/** When anything last touched a page (the page server's updated_at), epoch seconds. */
export function touchedAt(sf: Surface, statuses: Record<string, SessionStatus>): number {
  return isoEpoch(statusOf(sf, statuses).updated_at);
}

/** statusd.starting_rows: what became of a launch with no page yet, in words. */
export function startingWhat(st: Pageless, now: number, doing: Record<string, string>): string {
  if (st.running) {
    const said = hasKey(doing, st.task) ? doing[st.task] : '';
    return said ? `working — ${said}` : 'working — its page appears when this round ends';
  }
  if (st.ran) return 'ran, but wrote no page';
  if (st.failed) return `could not start — ${shortError(st.failed)}`;
  if (st.started) return 'started, then stopped before writing a page';
  if (now - num(st.since) > STARTUP_GRACE) return 'never started — the runner did not pick this up';
  return 'queued — the runner starts it on its next pass';
}

/** statusd.run_library: what a skill picker offers from a library scan. */
export function runLibrary(entries: InstalledEntry[]): LibraryEntry[] {
  return entries.filter((e) => !e.broken)
    .sort((a, b) => cmp(a.kind, b.kind) || cmp(a.name.toLowerCase(), b.name.toLowerCase()))
    .map((e) => ({ v: `${e.kind}:${e.name}`, i: e.invoke, k: e.kind, d: [...(e.description || '')].slice(0, 120).join('') }));
}

