// layout() as the Python page script runs it against the DOM: find reads
// each item's visible text, and each layout starts from the order the last
// one left the items in, until the next re-fetch redraws them.

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { layout, type Home, type LayoutInput, type Placed, type Sel } from '../model';

/** The placed list, and the ref callback each item's element registers with (for its text). */
export interface WorkLayout {
  placed: Placed;
  textRef: (key: string, el: HTMLElement | null) => void;
}

export function useWorkLayout(home: Home | null, sel: Sel, view: Omit<LayoutInput, 'text'>): WorkLayout | null {
  const els = useRef(new Map<string, HTMLElement>());
  const textRef = useCallback((key: string, el: HTMLElement | null) => {
    if (el) els.current.set(key, el);
    else els.current.delete(key);
  }, []);
  // After a re-fetch the first layout matched find against the old DOM text;
  // once the new items are drawn, lay out once more against their text. It
  // hangs on `home` alone: the text moved, not the hook's arguments.
  const [, rerender] = useState(0);
  useEffect(() => { if (view.q) rerender((n) => n + 1); }, [home]); // eslint-disable-line react-hooks/exhaustive-deps

  const left = useRef<{ from: Home | null; keys: string[] }>({ from: null, keys: [] });
  let items = home?.items ?? [];
  if (home && left.current.from === home) {
    const at = new Map(left.current.keys.map((k, i) => [k, i]));
    items = [...items].sort((a, b) => (at.get(a.key) ?? -1) - (at.get(b.key) ?? -1));
  }
  const placed = home ? layout(items, sel, { ...view, text: (it) => els.current.get(it.key)?.textContent || it.title }) : null;
  useLayoutEffect(() => {
    if (placed) left.current = { from: home, keys: [...placed.list, ...placed.idle, ...placed.off].map((it) => it.key) };
  });
  return placed ? { placed, textRef } : null;
}
