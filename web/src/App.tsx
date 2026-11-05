import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchFleet, subscribe, type Health } from './api';
import { ago, elapsed, needsYou, recentlyFinished, running, usd } from './fleet';
import { NewWorkForm } from './NewWorkForm';
import type { Fleet } from './types';

const NEEDS_SHOWN = 12;

function useNow(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

function useFleet() {
  const [fleet, setFleet] = useState<Fleet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health>('connecting');
  const inflight = useRef<AbortController | null>(null);

  const refetch = useCallback(() => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    fetchFleet(ac.signal)
      .then((f) => {
        setFleet(f);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!ac.signal.aborted) setError(e instanceof Error ? e.message : String(e));
      });
  }, []);

  useEffect(() => {
    refetch();
    const stop = subscribe(refetch, setHealth);
    return () => {
      stop();
      inflight.current?.abort();
    };
  }, [refetch]);

  return { fleet, error, health, refetch };
}

const HEALTH_LABEL: Record<Health, string> = {
  connecting: 'connecting',
  live: 'live',
  degraded: 'backend degraded',
  offline: 'offline',
};

export function App() {
  const { fleet, error, health, refetch } = useFleet();
  const now = useNow(15_000);
  const [showAll, setShowAll] = useState(false);

  const needs = fleet ? needsYou(fleet) : [];
  const live = fleet ? running(fleet) : [];
  const recent = fleet ? recentlyFinished(fleet) : [];
  const shownNeeds = showAll ? needs : needs.slice(0, NEEDS_SHOWN);

  return (
    <div className="page">
      <header className="top">
        <h1>
          Cadre <span className="machine">{fleet?.machine ?? '…'}</span>
        </h1>
        <div className="meta">
          <span className="spend" title="Spend over the last 7 days">
            {fleet ? `${usd(fleet.week_usd)} this week` : ''}
          </span>
          <span className={`dot ${health}`} role="status" aria-label={HEALTH_LABEL[health]}>
            {HEALTH_LABEL[health]}
          </span>
        </div>
      </header>

      {error && <p className="banner">Could not load the fleet: {error}</p>}

      <main className="grid">
        <section className="needs" aria-labelledby="h-needs">
          <h2 id="h-needs">
            Needs you <span className="count">{needs.length}</span>
          </h2>
          {fleet && needs.length === 0 && <p className="empty">Nothing is waiting on you.</p>}
          <ul className="rows">
            {shownNeeds.map((r) => (
              <li key={r.key} className={r.unseen ? 'row unseen' : 'row'}>
                <a href={r.path}>
                  {r.unseen && <span className="pip" title="New since you last looked" />}
                  <span className="title">{r.title}</span>
                </a>
                <span className="sub">
                  {r.project && <span className="proj">{r.project}</span>}
                  <span>{r.replied ? 'agent replied — your turn' : 'awaiting you'}</span>
                  <span>{ago(r.since, now)}</span>
                </span>
              </li>
            ))}
          </ul>
          {needs.length > NEEDS_SHOWN && (
            <button className="link" onClick={() => setShowAll((v) => !v)}>
              {showAll ? 'Show fewer' : `Show all ${needs.length}`}
            </button>
          )}
        </section>

        <div className="side">
          <section className="running" aria-labelledby="h-running">
            <h2 id="h-running">
              Running{' '}
              <span className="count">
                {live.filter((c) => !c.waiting).length}
                {fleet?.slots ? ` / ${fleet.slots}` : ''}
              </span>
            </h2>
            {fleet && live.length === 0 && <p className="empty">No agents running.</p>}
            <ul className="rows">
              {live.map((c) => (
                <li key={c.task} className="row">
                  {c.page ? (
                    <a href={c.page}>
                      <span className="title">{c.title || c.item_title || c.task}</span>
                    </a>
                  ) : (
                    <span className="title">{c.title || c.item_title || c.task}</span>
                  )}
                  <span className="doing">{c.waiting ? 'waiting for a slot' : c.doing || 'starting'}</span>
                  <span className="sub">
                    <span className="mono">{c.folder}</span>
                    {c.started > 0 && <span>{elapsed(c.started, now)}</span>}
                  </span>
                </li>
              ))}
            </ul>
          </section>
  
          <section className="new" aria-labelledby="h-new">
            <h2 id="h-new">New work</h2>
            <NewWorkForm onSubmitted={refetch} />
          </section>
        </div>

        <section className="recent" aria-labelledby="h-recent">
          <h2 id="h-recent">Recently finished</h2>
          {fleet && recent.length === 0 && <p className="empty">Nothing finished lately.</p>}
          <ul className="rows">
            {recent.map((r) => (
              <li key={`${r.task}-${r.ended}`} className="row">
                {r.path ? (
                  <a href={r.path}>
                    <span className="title">{r.title}</span>
                  </a>
                ) : (
                  <span className="title">{r.title}</span>
                )}
                <span className="sub">
                  <span className="proj">{r.project}</span>
                  <span className={r.ok ? '' : 'bad'}>{r.ok ? 'done' : 'failed'}</span>
                  <span>{ago(r.ended, now)}</span>
                  <span>{usd(r.cost)}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>

      </main>
    </div>
  );
}
