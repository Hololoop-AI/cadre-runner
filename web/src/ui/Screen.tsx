// The frame every page but home has on the Python side: no body class, the
// page's name with the accent dot, and its title in the tab. Plus the
// fetch-once hook each of those screens reads its data with.

import { useEffect, useState, type ReactNode } from 'react';

/** `<div class="wrap"><header><h1>Name.</h1></header>…` with the tab title (`dot` false: no accent dot, as the manage page). */
export function Screen({ name, after = '', dot = true, tab, children }: {
  name: string; after?: string; dot?: boolean; tab: string; children: ReactNode;
}) {
  useEffect(() => {
    document.title = tab;
  }, [tab]);
  return (
    <div className="wrap">
      <header><h1>{name}{dot && <span className="dot">.</span>}{after}</h1></header>
      {children}
    </div>
  );
}

/** One GET for a screen: the answer, or the error that stopped it. */
export function useOnce<T>(load: () => Promise<T>, key: string): { data: T | null; error: string } {
  const [state, setState] = useState<{ data: T | null; error: string }>({ data: null, error: '' });
  useEffect(() => {
    let live = true;
    load().then((data) => live && setState({ data, error: '' }))
      .catch((e: unknown) => live && setState({ data: null, error: e instanceof Error ? e.message : String(e) }));
    return () => {
      live = false;
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

/** What a screen shows while its data has not come, or could not. */
export function Pending({ error }: { error: string }) {
  return error ? <p className="notice">{`Could not load this page: ${error}`}</p> : null;
}
