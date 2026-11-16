// Hololoop, the fleet's home at /: the orchestrator's view. It sees cadres,
// not their runs: one card per cadre saying how much waits on the driver,
// how much is running and what the week cost, each opening that cadre's own
// app (/c/<id>). The rail down the left is the same one every cadre shows.
// An old link to /?group=<id> lands in that cadre's app instead.

import { useEffect, useMemo } from 'react';
import { buildHome, cadreCards, groupBadges, HOME_GROUP, serverView, usd } from '../model';
import { cadreHref, railPick } from './Cadre';
import { GroupBar } from './Groups';
import { GroupIcon } from './GroupIcon';
import { go } from './router';
import { MachineSwitch, useServer, type Machines } from './server';
import { useFleet } from './useFleet';
import './shell.css';

export function Hololoop({ machines }: { machines: Machines }) {
  const server = useServer();
  const { fleet, now, live, error } = useFleet(server);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const g = q.get('group');
    if (g) go(cadreHref(g) + (q.get('parent') ? `?parent=${encodeURIComponent(q.get('parent')!)}` : ''));
    document.title = 'Cadre — Hololoop';
  }, []);
  const home = useMemo(() => (fleet ? buildHome(fleet, serverView(''), now, server.base || location.origin) : null), [fleet, now, server]);
  const groups = home?.groups ?? [];
  const cards = useMemo(() => (home && fleet ? cadreCards(home.items, home.member, home.groups, fleet) : []), [home, fleet]);
  const badges = useMemo(() => (home ? groupBadges(home.items, home.member, groups) : {}), [home, groups]);
  return (
    <>
      <GroupBar groups={groups} group={HOME_GROUP} badges={badges} shut={false} open={false} onPick={railPick} onShut={() => {}} />
      <div className="wrap holo">
        <header>
          <h1>Cadre<span className="dot">.</span><span className="group">hololoop</span></h1>
          <MachineSwitch {...machines} /><span className="sp"></span>
          {fleet?.week_usd != null && <a href="/costs">{`${usd(fleet.week_usd)} in 7 days`}</a>}
          <a href="/runs">every run</a><a href="/cards">cards</a>
          <span className={live === 'connecting' ? 'live' : `live ${live}`}>{live === 'off' ? '○ reconnecting' : live === 'connecting' ? 'connecting…' : '● live'}</span>
        </header>
        {error && <p className="notice">{`Could not load the fleet: ${error}`}</p>}
        <h2 className="sect">Cadres</h2>
        {home && !cards.length && <p className="meta">No cadres yet. A cadre is a project other projects belong to: <code>pipeline.py project set &lt;project&gt; --group &lt;cadre&gt;</code>.</p>}
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
        <p className="meta">Hololoop sees cadres, not their runs: open one to see its work.</p>
        <footer>
          <a href="/library">installed skills, agents and workflows</a> · <a href="/tree">which session launched which</a> · <a href="/costs">what each run cost</a>
        </footer>
      </div>
    </>
  );
}
