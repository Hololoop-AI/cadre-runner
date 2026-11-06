// /costs: statusd.render_costs — totals, the same spend by node, project and
// task, then the newest turns one by one.

import type { ReactNode } from 'react';
import { num, plural, pyRound, relTime, s, taskLabel, usd, type CostBucket, type TurnRecord } from '../model';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

/** costs.cost_of: a turn's reported cost, or null when it reported none. */
function costOf(e: TurnRecord): number | null {
  const c = e.cost_usd;
  return typeof c === 'number' && Number.isFinite(c) ? c : null;
}

/** statusd._cost_rows */
function bucketRows(buckets: CostBucket[], label: (b: CostBucket) => ReactNode, limit?: number) {
  return buckets.slice(0, limit).map((b, i) => {
    const unpriced = num(b.unpriced);
    const avg = b.usd / Math.max(1, b.runs - unpriced);
    return (
      <tr key={i}>
        <td className="desc">{label(b)}</td>
        <td className="inv">{usd(b.usd)}</td>
        <td className="scope">{`${b.runs} turn${plural(b.runs)}${unpriced ? ` · ${unpriced} unpriced` : ''} · ${usd(avg)} avg`}</td>
      </tr>
    );
  });
}

function Card({ title, rows }: { title: string; rows: ReactNode[] }) {
  return rows.length ? (
    <section className="card"><h2>{title}</h2><table className="lib"><tbody>{rows}</tbody></table></section>
  ) : null;
}

/** The costs screen. `now` is the moment the page was drawn, epoch seconds. */
export function CostsScreen() {
  const server = useServer();
  const { data: c, error } = useOnce(() => server.fetchCosts(), server.base);
  const now = Date.now() / 1000;
  return (
    <Screen name="Costs" tab="Cadre — costs">
      <p className="meta"><a href="/">← fleet</a> · <a href="/costs.json">as JSON</a></p>
      {!c ? <Pending error={error} /> : (
        <>
          <section className="card"><h2>Spent</h2>
            <p><b>{usd(c.total_usd)}</b>{` across ${c.priced} priced turns · `}<b>{usd(c.last_7d_usd)}</b>{' in the last 7 days · '}<b>{usd(c.last_24h_usd)}</b> in the last 24 hours.</p>
            <p className="empty">{"A turn's cost is what Claude Code reported when it ended, so a turn still running shows up here once it finishes."
              + (c.unpriced ? ` ${c.unpriced} turn${plural(c.unpriced)} reported no cost (lost, timed out, or paused on a usage limit) and are left out of the sums.` : '')}</p>
          </section>
          <Card title="By node" rows={bucketRows(c.by_node, (b) => b.key)} />
          <Card title="By project" rows={bucketRows(c.by_project, (b) => b.key)} />
          <Card title="By task · most expensive first" rows={bucketRows(c.by_task, (b) => (
            <>{`${taskLabel(b.key)} `}<span className="scope">{b.project || ''}</span></>), 40)} />
          <Card title="Newest turns" rows={c.recent.map((e, i) => {
            const cost = costOf(e);
            return (
              <tr key={i}>
                <td className="desc">{taskLabel(s(e.story))}</td>
                <td className="inv">{cost !== null ? usd(cost) : '—'}</td>
                <td className="scope">{`${s(e.stage)} · ${s(e.model)} · ${pyRound(num(e.seconds) / 60)} min · ${relTime(num(e.ended), now)}${e.ok ? '' : ' · failed'}`}</td>
              </tr>
            );
          })} />
        </>
      )}
    </Screen>
  );
}
