// The groups sidebar (model/groups.ts): All, each group, then Other, down
// the left edge the way Discord lists servers. Choosing one filters the Work
// list. React only, so its look lives in groups.css, not the generated
// fleet.css; with no group defined it draws nothing. Its top button folds
// it to a thin rail and back; the browser keeps that with the view (Sel).

import { OTHER, type Group } from '../model';
import './groups.css';

/** Two letters for the round badge: "Everyday" -> "Ev", "Home server" -> "HS". */
function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) return (words[0][0] + words[1][0]).toUpperCase();
  return [...(words[0] || '?')].slice(0, 2).join('');
}

export function GroupBar({ groups, group, badges, shut, onPick, onShut }: {
  groups: Group[]; group: string; badges: Record<string, number>; shut: boolean;
  onPick: (id: string) => void; onShut: (shut: boolean) => void;
}) {
  if (!groups.length) return null;
  const entries: Group[] = [{ id: '', name: 'All' }, ...groups, { id: OTHER, name: 'Other' }];
  const fold = (
    <button type="button" className="gfold" aria-expanded={!shut}
      title={shut ? 'Show the groups' : 'Fold the groups away'} onClick={() => onShut(!shut)}>
      {shut ? '›' : '‹'}
    </button>
  );
  if (shut) return <nav className="groups shut" aria-label="Groups of work">{fold}</nav>;
  return (
    <nav className="groups" aria-label="Groups of work">
      {fold}
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
