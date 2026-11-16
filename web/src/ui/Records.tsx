// A cadre's Records tab: the HTML pages it keeps (runnerlib/records.py) —
// decision records, a pantry list, recipes — grouped by type, newest first.
// Each opens as a review page through the page server (/open?file=), so it
// can be read, annotated and edited there. Agents save them with
// `pipeline.py records add <page.html> --cadre <id> --type <type>`.

import { relTime, type CadreRecord } from '../model';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

export function RecordsScreen({ cadre, name }: { cadre: string; name: string }) {
  const server = useServer();
  const { data, error } = useOnce(() => server.fetchRecords(cadre), `${server.base}|records|${cadre}`);
  const now = Date.now() / 1000;
  const byType = new Map<string, CadreRecord[]>();
  for (const r of data?.records || []) byType.set(r.type, [...(byType.get(r.type) || []), r]);
  return (
    <Screen name="Records" tab="Cadre — records">
      <p className="meta">{`The pages ${name} keeps. Each opens as a review page you can read and annotate. Agents save them with `}
        <code>{`pipeline.py records add <page.html> --cadre ${cadre} --type <type>`}</code>{'.'}</p>
      {!data ? <Pending error={error} />
        : !data.records.length ? <p className="meta">{`${name} keeps no records yet.`}</p>
          : [...byType.entries()].map(([type, recs]) => (
            <section key={type || '_'} className="rtype">
              <h3>{type || 'untyped'} <span className="kn">{recs.length}</span></h3>
              <ul className="records">
                {recs.map((r) => (
                  <li key={r.id}>
                    <a href={r.href}>{r.name.replace(/\.html?$/, '')}</a>
                    {r.note && <span className="meta">{` — ${r.note}`}</span>}
                    <span className="when">{relTime(r.created_at, now)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
    </Screen>
  );
}
