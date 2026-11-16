// A cadre's Blackboard tab: the newest events on the blackboard, the store
// agents write to each other (runnerlib/blackboard.py; GET /api/blackboard).
// It is not the kanban board: the kanban board holds cards a person and
// agents move by hand; the blackboard holds the events that hand work
// between agents and nodes (handoffs, asks and answers, signals). Read only.

import { relTime, type BlackboardEvent } from '../model';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

/** The event's payload in one line: its fields, the long ones cut. */
function gist(ev: BlackboardEvent): string {
  return Object.entries(ev.payload || {})
    .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(' · ').slice(0, 240);
}

/** Who wrote it: the session or task its provenance names, else the namespace. */
function who(ev: BlackboardEvent): string {
  const p = ev.provenance || {};
  return String(p.agent || p.stage || p.session_id || p.machine || '') || '—';
}

export function BlackboardScreen() {
  const server = useServer();
  const { data, error } = useOnce(() => server.fetchBlackboard(), server.base + '|blackboard');
  const now = Date.now() / 1000;
  return (
    <Screen name="Blackboard" tab="Cadre — blackboard">
      <p className="meta">{'The newest events agents and nodes wrote to each other: handoffs, asks and answers, signals. '}
        {'Cards you move by hand are on the Kanban tab; agents read these with '}<code>pipeline.py</code>{'.'}</p>
      {!data ? <Pending error={error} />
        : !data.events.length ? <p className="meta">Nothing on the blackboard yet.</p>
          : (
            <table className="bboard">
              <thead><tr><th>when</th><th>kind</th><th>where</th><th>what</th><th>by</th></tr></thead>
              <tbody>
                {data.events.map((ev) => (
                  <tr key={ev.id}>
                    <td>{relTime(ev.ts, now)}</td>
                    <td><span className="badge">{ev.kind}</span></td>
                    <td><code>{`${ev.namespace}/${ev.topic}/${ev.key}`}</code></td>
                    <td>{gist(ev)}</td>
                    <td>{who(ev)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
    </Screen>
  );
}
