// Which machine the app is looking at. The app loads from one fleet server
// and can switch to any machine that server lists (/api/backends): one at a
// time, never all on one page. Every read, post and stream then goes to the
// chosen machine directly, and links on the page point at it.

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { connect, fetchBackends, type Server } from '../api';
import type { Backend } from '../model';

const ServerContext = createContext<Server>(connect(''));

/** The chosen machine's API. */
export function useServer(): Server {
  return useContext(ServerContext);
}

/** Where the choice is kept in this browser. */
const KEY = 'cadre.machine';

/** The machines to switch between, the chosen one's origin, and how to choose. */
export interface Machines {
  machines: Backend[];
  base: string;
  choose: (base: string) => void;
}

export function useMachines(): Machines {
  const [machines, setMachines] = useState<Backend[]>([]);
  const [base, setBase] = useState(() => {
    try {
      return window.localStorage.getItem(KEY) || '';
    } catch {
      return '';
    }
  });
  useEffect(() => { fetchBackends().then(setMachines).catch(() => {}); }, []);
  // a kept choice this server no longer lists falls back to this machine
  const known = !base || machines.length === 0 || machines.some((m) => m.url === base);
  const choose = (next: string) => {
    setBase(next);
    try {
      if (next) window.localStorage.setItem(KEY, next);
      else window.localStorage.removeItem(KEY);
    } catch {
      /* private mode: the choice lasts this page */
    }
  };
  return { machines, base: known ? base : '', choose };
}

/**
 * Provide the chosen machine's API, and aim the page's root-relative links
 * at it: a <base> pointing at another machine opens its pages there, in a
 * new tab, as the Python page's links to another machine do.
 */
export function ServerProvider({ base, children }: { base: string; children: ReactNode }) {
  const server = useMemo(() => connect(base), [base]);
  useEffect(() => {
    if (!base) return;
    const el = document.createElement('base');
    el.href = base + '/';
    el.target = '_blank';
    document.head.prepend(el);
    return () => el.remove();
  }, [base]);
  return <ServerContext.Provider value={server}>{children}</ServerContext.Provider>;
}

/** The switcher in the header: shown only when there is more than one machine. */
export function MachineSwitch({ machines, base, choose }: Machines) {
  if (machines.length < 2) return null;
  return (
    <select id="machine" aria-label="machine" data-beyond-parity="" value={base}
      onChange={(e) => choose(e.target.value)}>
      {machines.map((m) => <option key={m.url || 'here'} value={m.url}>{m.name}</option>)}
    </select>
  );
}
