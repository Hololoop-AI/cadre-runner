// The groups sidebar (model/groups.ts): All, each group, then Other, down
// the left edge the way Discord lists servers. Choosing one filters the Work
// list. React only, so its look lives in groups.css, not the generated
// fleet.css; with no group defined it draws nothing.

import { OTHER, type Group } from '../model';
import './groups.css';

/** Two letters for the round badge: "Everyday" -> "Ev", "Home server" -> "HS". */
function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) return (words[0][0] + words[1][0]).toUpperCase();
  return [...(words[0] || '?')].slice(0, 2).join('');
}

export function GroupBar({ groups, group, badges, onPick }: {
  groups: Group[]; group: string; badges: Record<string, number>; onPick: (id: string) => void;
}) {
  if (!groups.length) return null;
  const entries: Group[] = [{ id: '', name: 'All' }, ...groups, { id: OTHER, name: 'Other' }];
  return (
    <nav className="groups" aria-label="Groups of work">
      {entries.map((g) => (
        <button key={g.id || 'all'} type="button" className={g.id === group ? 'on' : ''} aria-pressed={g.id === group}
          title={g.id === OTHER ? 'Work in no group: its project has none and no tag names one' : `Show ${g.name}`}
          onClick={() => onPick(g.id)}>
          <span className="gi">{g.id === '' ? 'All' : g.id === OTHER ? '…' : initials(g.name)}</span>
          <span className="gn">{g.name}</span>
          {!!badges[g.id] && <span className="gb" title="items with a page you have not opened">{badges[g.id]}</span>}
        </button>
      ))}
    </nav>
  );
}
