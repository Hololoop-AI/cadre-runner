// The groups sidebar (model/groups.ts): Home (everything), each group, then
// Other, down the left edge the way Discord lists servers. Choosing one
// filters the Work list. React only, so its look lives in groups.css, not the
// generated fleet.css; with no group defined it draws nothing.
//
// On a wide screen its top button folds it to a thin rail and back; the
// browser keeps that with the view (Sel). On a phone it is hidden: the
// header's group button (GroupMenuButton) opens it, and picking a group
// closes it again.

import { OTHER, type Group } from '../model';
import './groups.css';

/** The entry that shows every item. The driver's word for the whole fleet is Home. */
const EVERYTHING: Group = { id: '', name: 'Home' };

/** Two letters for the round badge: "Personal" -> "Pe", "Home server" -> "HS". */
function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) return (words[0][0] + words[1][0]).toUpperCase();
  return [...(words[0] || '?')].slice(0, 2).join('');
}

/** What the chosen sidebar entry is called: Home, Other, or the group's name. */
function chosenName(groups: Group[], group: string): string {
  if (!group) return EVERYTHING.name;
  if (group === OTHER) return 'Other';
  return groups.find((g) => g.id === group)?.name ?? EVERYTHING.name;
}

export function GroupBar({ groups, group, badges, shut, open, onPick, onShut }: {
  groups: Group[]; group: string; badges: Record<string, number>; shut: boolean;
  /** a phone has opened it from the header */
  open: boolean;
  onPick: (id: string) => void; onShut: (shut: boolean) => void;
}) {
  if (!groups.length) return null;
  const entries: Group[] = [EVERYTHING, ...groups, { id: OTHER, name: 'Other' }];
  return (
    <nav className={['groups', shut && 'shut', open && 'open'].filter(Boolean).join(' ')} aria-label="Groups of work">
      <button type="button" className="gfold" aria-expanded={!shut}
        title={shut ? 'Show the groups' : 'Fold the groups away'} onClick={() => onShut(!shut)}>
        {shut ? '›' : '‹'}
      </button>
      {entries.map((g) => (
        <button key={g.id || 'home'} type="button" className={g.id === group ? 'on' : ''} aria-pressed={g.id === group}
          title={g.id === OTHER ? 'Work in no group: its project has none and no tag names one'
            : g.id === '' ? 'Show everything' : `Show ${g.name}`}
          onClick={() => onPick(g.id)}>
          <span className="gi">{g.id === '' ? '⌂' : g.id === OTHER ? '…' : initials(g.name)}</span>
          <span className="gn">{g.name}</span>
          {!!badges[g.id] && <span className="gb" title="items with a page you have not opened">{badges[g.id]}</span>}
        </button>
      ))}
    </nav>
  );
}

/** The header's group button, shown on a phone only: the chosen group's name; it opens the groups. */
export function GroupMenuButton({ groups, group, open, onToggle }: {
  groups: Group[]; group: string; open: boolean; onToggle: () => void;
}) {
  if (!groups.length) return null;
  return (
    <button type="button" className="gmenu" aria-expanded={open} title="Switch group" onClick={onToggle}>
      {chosenName(groups, group)} {open ? '▴' : '▾'}
    </button>
  );
}
