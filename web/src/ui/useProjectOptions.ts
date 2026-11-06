// The project select's options as the Python page keeps them: the live
// projects the server drew into the toolbar when the page loaded, then each
// other project an item names, added the first time an item names it and
// kept for the page's life, so a filter on it survives the item leaving.

import { useRef } from 'react';
import type { Home } from '../model';

/** `reloads` starts the list over, as a page reload does. */
export function useProjectOptions(home: Home | null, reloads: number): [string, string][] {
  const kept = useRef<{ reloads: number; options: [string, string][] } | null>(null);
  if (home && (!kept.current || kept.current.reloads !== reloads)) {
    kept.current = { reloads, options: [...home.projects] };
  } else if (home && kept.current) {
    // after the first draw only items add options (the toolbar is not redrawn)
    const have = new Set(kept.current.options.map(([k]) => k));
    const named = new Set(home.items.map((it) => it.project));
    const added = home.projects.filter(([k]) => named.has(k) && !have.has(k));
    if (added.length) kept.current.options = [...kept.current.options, ...added];
  }
  return kept.current?.options ?? [];
}
