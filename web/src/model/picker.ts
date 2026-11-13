// The New work dialog's two lookups, as the Python page's scripts do them:
// the skill picker's ranking (statusd._PICKER_JS) and which project a typed
// folder files under (the page script's hint()).

import type { LibraryEntry, Project } from './payload';
import { homePath, s } from './format';
import { liveProjects } from './projects';

const HINT = 'The project is the one that owns this folder; a folder no project owns becomes a new project.';

/** The picker's score: -1 for no match; hits after a separator and adjacent hits count double. */
export function score(hay: string, needle: string): number {
  const h = hay.toLowerCase();
  const n = needle.toLowerCase();
  let i = 0;
  let sc = 0;
  let last = -2;
  for (const c of n) {
    const at = h.indexOf(c, i);
    if (at < 0) return -1;
    let bonus = 1;
    if (at === last + 1) bonus += 2;
    if (at === 0 || '-_/: .'.includes(h[at - 1])) bonus += 2;
    sc += bonus;
    last = at;
    i = at + 1;
  }
  return sc - h.length * 0.01;
}

/** Names first, descriptions only as a plain-substring fallback. */
export function findEntries(lib: LibraryEntry[], q: string): LibraryEntry[] {
  const v = q.trim();
  if (!v) return [];
  const hits = lib.map((e) => ({ e, s: score(e.i, v) })).filter((r) => r.s > 0)
    .sort((a, b) => b.s - a.s).slice(0, 12).map((r) => r.e);
  if (hits.length) return hits;
  const n = v.toLowerCase();
  return lib.filter((e) => e.d.toLowerCase().includes(n)).slice(0, 12);
}

/**
 * Which project a typed folder files under. A string-prefix match on
 * folders, as the page script does it, not projects.forDir's path-part
 * match: the two agree except on odd paths, and each copies its own original.
 * The words are the page script's hint().
 */
export function projectHint(value: string, home: string, projs: Record<string, Project>): string {
  let v = value.trim().replace(/\/+$/, '');
  if (!v) return HINT;
  if (v[0] === '~') v = home + v.slice(1);
  let best: [string, string] | null = null;
  for (const p of liveProjects(projs)) {
    for (const d of p.dirs || []) {
      const dir = String(d).replace(/\/+$/, '');
      if ((v === dir || v.startsWith(dir + '/')) && (!best || dir.length > best[0].length)) best = [dir, p.name];
    }
  }
  return best ? `Files under project “${best[1]}” (from the folder).`
    : 'No project owns this folder yet: it starts one named after the folder.';
}


/** The folder buttons under the field: each live project's first folder, written with ~. */
export function folderPicks(projs: Record<string, Project>, home: string): string[] {
  return [...new Set(liveProjects(projs).map((p) => s(p.dirs?.[0])))]
    .filter(Boolean).map((d) => homePath(d, home));
}
