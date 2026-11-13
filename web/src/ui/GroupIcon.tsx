// The sidebar's icons, in Hololoop's neon style: thin round-capped strokes in
// the current text colour, an outer ring like the Hololoop mark's, a glyph
// inside it, and a soft glow (groups.css). Drawn in currentColor, so they
// follow the theme: whatever colour the button has, the icon has.
//
// A group gets its glyph by name, for the groups this fleet knows (Dev,
// Kitchen, Personal); any other group gets its first letter inside the ring.

import type { ReactNode } from 'react';
import { HOME_GROUP, OTHER } from '../model';

/** The glyphs inside the ring, on a 32-unit square (the Hololoop mark's). */
const GLYPHS = new Map<string, ReactNode>(Object.entries({
  // everything: the Hololoop mark itself, three rings fading inwards
  // (hololoop-site RingMark.astro: middle 0.64 and inner 0.3 of the outer, at 60% and ~35%)
  [HOME_GROUP]: <><circle className="rm" cx="16" cy="16" r="8.3" opacity="0.6" /><circle className="ri" cx="16" cy="16" r="3.9" opacity="0.35" /></>,
  // dev: </>
  dev: <path d="M12 11.5 7.5 16l4.5 4.5M20 11.5l4.5 4.5-4.5 4.5M17.4 10.5l-2.8 11" />,
  // kitchen: a pot with handles and a lid
  kitchen: <path d="M10 15.5v4.5a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3v-4.5zM7.5 17h2.5M22 17h2.5M9.5 13h13M16 10.5V13" />,
  // personal: a head and shoulders
  personal: <path d="M16 15.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM10.5 22.5a5.5 5.5 0 0 1 11 0" />,
  // other: three dots
  [OTHER]: <path d="M11 16h.01M16 16h.01M21 16h.01" strokeWidth="2.6" />,
}));

/** The glyph for a group: by its id or its name, lower-cased; else none. A Map, so a group named "constructor" gets none. */
function glyphFor(id: string, name: string): ReactNode | undefined {
  return GLYPHS.get(id) ?? GLYPHS.get(name.toLowerCase());
}

/** A sidebar entry's icon: the ring and the group's glyph (or its first letter). */
export function GroupIcon({ id, name }: { id: string; name: string }) {
  const glyph = glyphFor(id, name);
  // Home is the Hololoop mark, which breathes while chosen (groups.css)
  return (
    <svg className={id === HOME_GROUP ? 'gsvg breathe' : 'gsvg'} viewBox="0 0 32 32" aria-hidden="true" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle className="ro" cx="16" cy="16" r="13" />
      {glyph ?? <text x="16" y="20.5" textAnchor="middle" fontSize="12" fontWeight="600"
        fill="currentColor" stroke="none">{[...name.trim()][0]?.toUpperCase() ?? '?'}</text>}
    </svg>
  );
}
