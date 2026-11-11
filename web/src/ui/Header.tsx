// The home page's header (statusd.render_home): spend, cards, every run, the
// live dot the page script drives, and + New work.

import type { ReactNode } from 'react';
import type { Live } from '../api';
import { usd } from '../model';

const LIVE_TEXT: Record<Live, [string, string]> = {
  connecting: ['connecting…', ''],
  on: ['● live', 'Every machine is answering; updates within a couple of seconds'],
  half: ['● live', 'A machine is not answering, or cannot see its own review pages change; refreshes may lag'],
  off: ['○ reconnecting', 'Stream lost; refreshing every 30 s until it reconnects'],
};

/**
 * The header; `menu` (a phone's group button) and then `switcher` (the
 * machine select) sit after the title when there are any.
 */
export function Header({ weekUsd, live, snapshot, onNewWork, switcher, menu }: {
  weekUsd: number | undefined; live: Live; snapshot: string | undefined; onNewWork: () => void;
  switcher?: ReactNode; menu?: ReactNode;
}) {
  const [text, title] = LIVE_TEXT[live];
  return (
    <header>
      <h1>Cadre<span className="dot">.</span></h1>{menu}{switcher}<span className="sp"></span>
      {weekUsd != null && <a href="/costs">{`${usd(weekUsd)} in 7 days`}</a>}
      <a href="/cards">cards</a><a href="/runs">every run</a>
      <span id="live" className={live === 'connecting' ? 'live' : `live ${live}`}
        title={live === 'connecting' ? `snapshot ${snapshot || '—'}` : title}>{text}</span>
      <button type="button" className="primary" id="newworkbtn" onClick={onNewWork}>+ New work</button>
    </header>
  );
}
