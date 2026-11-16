// Holo, the fleet's home at /, is a cadre like any other, with runs of
// its own: the meta work on the cadres and the system (runnerlib/cadres.py
// HOLO). Its home (App.tsx Home) leads with this: one card per cadre saying
// how much waits on the driver, how much is running and what the week cost,
// each opening that cadre's own app (/c/<id>).

import { useEffect, useMemo } from 'react';
import { cadreCards, usd, type Fleet, type Home } from '../model';
import { cadreHref } from './Cadre';
import { GroupIcon } from './GroupIcon';
import { go } from './router';

/** An old link to /?group=<id> lands in that cadre's app instead. */
export function useHoloRedirect(on: boolean) {
  useEffect(() => {
    if (!on) return;
    const q = new URLSearchParams(window.location.search);
    const g = q.get('group');
    if (g) go(cadreHref(g) + (q.get('parent') ? `?parent=${encodeURIComponent(q.get('parent')!)}` : ''));
  }, [on]);
}

/** A card per cadre, each opening its app. */
export function CadreCardList({ fleet, home }: { fleet: Fleet; home: Home }) {
  const cards = useMemo(() => cadreCards(home.items, home.member, home.groups, fleet), [home, fleet]);
  return (
    <section aria-label="Cadres">
      <h2 className="sect">Cadres</h2>
      {!cards.length && <p className="meta">No cadres yet. A cadre is a project other projects belong to: <code>pipeline.py project set &lt;project&gt; --group &lt;cadre&gt;</code>.</p>}
      <ul className="cadres">
        {cards.map((c) => (
          <li key={c.id}>
            <a href={cadreHref(c.id)} className="ccard">
              <span className="gi"><GroupIcon id={c.id} name={c.name} /></span>
              <span className="cn">{c.name}</span>
              <span className={c.waiting ? 'cw on' : 'cw'}>{`${c.waiting} need${c.waiting === 1 ? 's' : ''} you`}</span>
              <span className="cr">{`${c.running} running`}</span>
              <span className="cu">{`${usd(c.weekUsd)} this week`}</span>
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
