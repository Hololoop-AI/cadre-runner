// /library: statusd.render_library — everything installed that a launch can
// pick, by kind.

import type { InstalledEntry } from '../model';
import { Pending, Screen, useOnce } from './Screen';
import { useServer } from './server';

const KINDS: [string, string][] = [['skill', 'Skills'], ['workflow', 'Workflows'], ['agent', 'Agents'], ['flow', 'Cadre flows']];

function Row({ e }: { e: InstalledEntry }) {
  return (
    <tr>
      <td className="inv">{e.invoke || e.name}</td>
      <td className="scope">{e.scope}</td>
      <td className="desc">{e.broken && <><b>broken link</b> </>}{[...(e.description || '')].slice(0, 180).join('')}</td>
    </tr>
  );
}

/** The library screen. */
export function LibraryScreen() {
  const server = useServer();
  const { data, error } = useOnce(() => server.fetchLibrary(), server.base);
  return (
    <Screen name="Library" tab="Cadre — library">
      {!data ? <Pending error={error} /> : (
        <>
          <p className="meta"><a href="/">← fleet</a>{` · found by scanning ~/.claude, the runner's skills tree and these project directories: ${data.scanned.join(', ') || 'none'}`}</p>
          <section className="card"><p className="empty">Launch any of these from a project page, or <code>pipeline.py task "…" --run kind:name</code>. Skills and workflows start as <code>/name</code> at the front of the prompt, agents as <code>@agent-name</code>; flows are listed for reference — every launch runs as a dialogue today.</p></section>
          {KINDS.map(([kind, label]) => {
            const rows = data.entries.filter((e) => e.kind === kind);
            return rows.length ? (
              <section key={kind} className="card"><h2>{`${label} · ${rows.length}`}</h2>
                <table className="lib"><tbody>{rows.map((e, i) => <Row key={i} e={e} />)}</tbody></table>
              </section>
            ) : null;
          })}
        </>
      )}
    </Screen>
  );
}
