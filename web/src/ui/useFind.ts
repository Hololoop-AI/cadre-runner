// Find everywhere: every page ever opened, not just the listed items, once
// the find box holds two characters (as the Python page asks /find).

import { useEffect, useState } from 'react';
import { findPages, type Hit, type Home } from '../model';

/** The hits and the moment they were found; null while the box holds fewer than two characters. */
export function useFind(q: string, home: Home | null): { hits: Hit[]; now: number } | null {
  const [found, setFound] = useState<{ hits: Hit[]; now: number } | null>(null);
  useEffect(() => {
    const v = q.trim();
    if (v.length < 2 || !home) {
      setFound(null);
      return;
    }
    const { surfaces, records, holders, pages } = home.find;
    const t = setTimeout(() => setFound({
      hits: findPages(v.slice(0, 200), surfaces, records, holders, pages), now: Date.now() / 1000,
    }), 200);
    return () => clearTimeout(t);
  }, [q, home]);
  return found;
}
