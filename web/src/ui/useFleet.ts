// This machine's fleet, kept current: GET /api/fleet once, then again each
// time the fleet stream says something changed.

import { useCallback, useEffect, useRef, useState } from 'react';
import type { Live, Server } from '../api';
import type { Fleet } from '../model';

/** The latest payload, the moment it arrived, the stream's health, and a manual re-fetch. */
export interface FleetState {
  fleet: Fleet | null;
  /**
   * The page's clock, epoch seconds: the moment the payload arrived. As on
   * the Python page, "3m ago" moves when the list is re-fetched, not on a timer.
   */
  now: number;
  live: Live;
  /** why the last fetch failed, '' after one succeeds */
  error: string;
  refetch: () => void;
}

/** The fleet of the machine `server` talks to; switching machines starts over. */
export function useFleet(server: Server): FleetState {
  const [got, setGot] = useState<{ fleet: Fleet | null; now: number }>({ fleet: null, now: Date.now() / 1000 });
  const [live, setLive] = useState<Live>('connecting');
  const [error, setError] = useState('');
  const inflight = useRef<AbortController | null>(null);
  const refetch = useCallback(() => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    server.fetchFleet(ac.signal).then((fleet) => {
      setGot({ fleet, now: Date.now() / 1000 });
      setError('');
    }).catch((e: unknown) => {
      if (!ac.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    });
  }, [server]);
  useEffect(() => {
    setGot({ fleet: null, now: Date.now() / 1000 });
    refetch();
    const stop = server.subscribe(refetch, setLive);
    return () => {
      stop();
      inflight.current?.abort();
    };
  }, [refetch, server]);
  return { ...got, live, error, refetch };
}
