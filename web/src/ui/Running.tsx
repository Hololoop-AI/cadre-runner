// The Running now strip: statusd.running_cards_html + running_section.

import { homePath, num, s, treeHref, viewHref, type RunningCard } from '../model';

function Card({ c, now, home }: { c: RunningCard; now: number; home: string }) {
  const title = s(c.title || c.task);
  let doing: string;
  if (c.waiting) doing = 'waiting for a slot';
  else {
    doing = s(c.doing || 'working');
    const n = Math.trunc(num(c.agents));
    if (n && !doing.includes('agent')) doing = `${n} subagent${n !== 1 ? 's' : ''} · ${doing}`;
  }
  const started = num(c.started);
  const deadline = num(c.deadline);
  const used = started ? Math.floor(Math.max(0, now - started) / 60) : 0;
  const budget = deadline > started ? Math.floor((deadline - started) / 60) : 0;
  const clock = budget ? `${used} min of ${budget}` : `${used} min`;
  const pct = budget ? Math.min(100, Math.trunc((used * 100) / budget)) : 0;
  return (
    <div className={`card agent${c.waiting ? ' waiting' : ''}`}>
      <div className="top"><span className="pulse"></span><span className="t" title={title}>{title}</span></div>
      <div className="doing">{doing}</div>
      <div className="pg">
        page: {c.page ? <a href={viewHref(c.page)}>{s(c.page_title || 'its page')}</a>
          : <span className="nopage">not written yet</span>}
        {c.item === c.task ? null : <> · under <a className="under" href={treeHref(s(c.item))}><b>{s(c.item_title || c.item)}</b></a></>}
      </div>
      <div className="pg" title="minutes this turn has used, against its deadline">{`${homePath(s(c.folder), home)} · ${clock}`}</div>
      <div className="bar"><i style={{ width: `${pct}%` }}></i></div>
    </div>
  );
}

export function RunningNow({ cards, slots, now, home }: { cards: RunningCard[]; slots: number; now: number; home: string }) {
  const list = cards.filter((c) => c && typeof c === 'object' && c.task);
  const nRun = list.filter((c) => !c.waiting).length;
  const nWait = list.length - nRun;
  let count = slots ? `${nRun} of ${slots} slots` : String(nRun);
  if (nWait) count += ` · ${nWait} waiting for a slot`;
  return (
    <section className="running-now">
      <h2>Running now <span className="n">{count}</span></h2>
      <div className="agents">
        {list.length ? list.map((c) => <Card key={c.task} c={c} now={now} home={home} />)
          : <p className="empty">No agent is running.</p>}
      </div>
    </section>
  );
}
