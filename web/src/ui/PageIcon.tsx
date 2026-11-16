// A cadre page's icon, in the rail's neon style (GroupIcon.tsx): thin
// round-capped strokes in currentColor with a soft glow (groups.css). A
// cadre's icon sits in a ring; a page's has none, so the two strips read
// apart at a glance. An unknown page gets its first letter.

import type { ReactNode } from 'react';

/** The glyphs, on the same 32-unit square as the cadre icons. */
const GLYPHS: Record<string, ReactNode> = {
  // home: a house
  home: <path d="M7 15.5 16 8l9 7.5M10 13.5V24h12V13.5M14 24v-5h4v5" />,
  // kanban: three columns of cards
  kanban: <path d="M7 8h5v16H7zM13.5 8h5v10h-5zM20 8h5v13h-5z" />,
  // blackboard: a board on an easel with a line of writing
  blackboard: <path d="M6 8h20v12H6zM10 12.5h7M10 16h11M12 20l-2.5 5M20 20l2.5 5" />,
  // records: a stack of pages
  records: <path d="M10 7h10l4 4v14H10zM20 7v4h4M13 15h8M13 18.5h8M13 22h5M7 10v17h13" />,
  // agents: a small robot head
  agents: <path d="M9 12h14v11H9zM16 12V8.5M16 7.5h.01M13 17h.01M19 17h.01M13 20.5h6M6.5 16v3M25.5 16v3" />,
  // settings: a gear, eight teeth round a hole
  settings: <><path d="M23.71 13.87L26.36 14.31L26.36 17.69L23.71 18.13L22.96 19.94L24.52 22.13L22.13 24.52L19.94 22.96L18.13 23.71L17.69 26.36L14.31 26.36L13.87 23.71L12.06 22.96L9.87 24.52L7.48 22.13L9.04 19.94L8.29 18.13L5.64 17.69L5.64 14.31L8.29 13.87L9.04 12.06L7.48 9.87L9.87 7.48L12.06 9.04L13.87 8.29L14.31 5.64L17.69 5.64L18.13 8.29L19.94 9.04L22.13 7.48L24.52 9.87L22.96 12.06Z" /><circle cx="16" cy="16" r="3.3" /></>,
};

export function PageIcon({ id, label }: { id: string; label: string }) {
  const glyph = GLYPHS[id];
  return (
    <svg className="gsvg" viewBox="0 0 32 32" aria-hidden="true" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      {glyph ?? <text x="16" y="20.5" textAnchor="middle" fontSize="13" fontWeight="600" fill="currentColor" stroke="none">
        {[...label.trim()][0]?.toUpperCase() ?? '?'}</text>}
    </svg>
  );
}
