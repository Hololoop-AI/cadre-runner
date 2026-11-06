// runnerlib/projects.py, the parts a renderer holding only the JSON works
// from: which projects are live, and which one owns a folder.

import { cmp, own } from './format';
import type { Project } from './payload';

/** projects.live: not archived, by name */
export function liveProjects(projs: Record<string, Project>): Project[] {
  return Object.values(projs)
    .filter((p) => !p.archived)
    .sort((a, b) => cmp(a.name.toLowerCase(), b.name.toLowerCase()));
}

export function namesOf(projs: Record<string, Project>, pid: string): string[] {
  const rec = own(projs, pid);
  return [...new Set([pid, String(rec?.name || pid)])];
}

function pathParts(p: string): string[] {
  return ['/', ...p.split('/').filter(Boolean)];
}

/** projects.for_dir: the live project with the deepest listed directory containing cwd */
export function forDir(projs: Record<string, Project>, cwd: string): Project | null {
  if (!cwd) return null;
  const c = pathParts(cwd);
  let best: Project | null = null;
  let depth = -1;
  for (const p of liveProjects(projs)) {
    for (const d of p.dirs || []) {
      const dp = pathParts(d);
      const inside = dp.length <= c.length && dp.every((x, i) => x === c[i]);
      if (inside && dp.length > depth) [best, depth] = [p, dp.length];
    }
  }
  return best;
}
