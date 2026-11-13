// The sidebar's icons, in Hololoop's neon style: thin round-capped strokes in
// the current text colour, an outer ring like the Hololoop mark's, a glyph
// inside it, and a soft glow (groups.css). Drawn in currentColor, so they
// follow the theme: whatever colour the button has, the icon has.
//
// A group gets its glyph by name, for the groups this fleet knows (Dev,
// Kitchen, Personal); any other group gets its first letter inside the ring.
// themeFavicon draws the page's tab icon the same way, in the theme's accent.

import type { ReactNode } from 'react';
import { OTHER } from '../model';

/** The glyphs inside the ring, on a 32-unit square (the Hololoop mark's). */
const GLYPHS: Record<string, ReactNode> = {
  // everything: the Hololoop mark itself, three rings fading inwards
  // (hololoop-site RingMark.astro: middle 0.64 and inner 0.3 of the outer, at 60% and ~35%)
  '': <><circle className="rm" cx="16" cy="16" r="8.3" opacity="0.6" /><circle className="ri" cx="16" cy="16" r="3.9" opacity="0.35" /></>,
  // dev: </>
  dev: <path d="M12 11.5 7.5 16l4.5 4.5M20 11.5l4.5 4.5-4.5 4.5M17.4 10.5l-2.8 11" />,
  // kitchen: a pot with handles and a lid
  kitchen: <path d="M10 15.5v4.5a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3v-4.5zM7.5 17h2.5M22 17h2.5M9.5 13h13M16 10.5V13" />,
  // personal: a head and shoulders
  personal: <path d="M16 15.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM10.5 22.5a5.5 5.5 0 0 1 11 0" />,
  // other: three dots
  [OTHER]: <path d="M11 16h.01M16 16h.01M21 16h.01" strokeWidth="2.6" />,
};

/** The glyph for a group: by its id or its name, lower-cased; else none. */
function glyphFor(id: string, name: string): ReactNode | undefined {
  return GLYPHS[id] ?? GLYPHS[name.toLowerCase()];
}

/** A sidebar entry's icon: the ring and the group's glyph (or its first letter). */
export function GroupIcon({ id, name }: { id: string; name: string }) {
  const glyph = glyphFor(id, name);
  // Home is the Hololoop mark, which breathes while chosen (groups.css)
  return (
    <svg className={id === '' ? 'gsvg breathe' : 'gsvg'} viewBox="0 0 32 32" aria-hidden="true" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle className="ro" cx="16" cy="16" r="13" />
      {glyph ?? <text x="16" y="20.5" textAnchor="middle" fontSize="12" fontWeight="600"
        fill="currentColor" stroke="none">{[...name.trim()][0]?.toUpperCase() ?? '?'}</text>}
    </svg>
  );
}

/** Set the tab icon to the Hololoop mark in the theme's accent, read from the page's --accent now. */
export function themeFavicon(doc: Document = document) {
  const accent = getComputedStyle(doc.documentElement).getPropertyValue('--accent').trim() || '#7fbbb3';
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='none' stroke='${accent}' stroke-width='2.8'>`
    + `<circle cx='16' cy='16' r='12.6'/><circle cx='16' cy='16' r='6' stroke-opacity='0.55'/></svg>`;
  let link = doc.querySelector<HTMLLinkElement>('link[rel~=icon]');
  if (!link) {
    link = doc.createElement('link');
    link.rel = 'icon';
    doc.head.appendChild(link);
  }
  link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/**
 * Keep the tab icon in the theme's accent: draw it now, and again whenever the
 * theme could have changed (the root element's style, class or data-theme, or
 * a stylesheet added to or swapped in the head). Returns the stop function.
 */
export function followThemeFavicon(doc: Document = document): () => void {
  let last = '';
  const draw = () => {
    const accent = getComputedStyle(doc.documentElement).getPropertyValue('--accent').trim();
    if (accent === last) return; // our own icon swap lands in the head too
    last = accent;
    themeFavicon(doc);
  };
  draw();
  const watch = new MutationObserver(draw);
  watch.observe(doc.documentElement, { attributes: true, attributeFilter: ['style', 'class', 'data-theme'] });
  watch.observe(doc.head, { childList: true, subtree: true, characterData: true });
  return () => watch.disconnect();
}
