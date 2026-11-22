// The way back: "← back" returns to the page you came from when it was a page
// of this app (history.back keeps its scroll and what was open), else goes up
// one level to `up`. "fleet" beside it is the quieter jump to the top.

import type { MouseEvent } from 'react';

/** Whether the previous history entry is a page of this app (same origin, and there is one). */
export function cameFromApp(doc: Document = document, hist: History = history): boolean {
  if (hist.length < 2 || !doc.referrer) return false;
  try {
    const from = new URL(doc.referrer);
    return from.origin === location.origin && from.href !== location.href;
  } catch {
    return false;
  }
}

export function BackLink({ up = '/', home = '/', target }: {
  /** where back goes when the page was not reached from this app */
  up?: string;
  /** where "fleet" goes */
  home?: string;
  target?: string;
}) {
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || !cameFromApp()) return;
    e.preventDefault();
    history.back();
  };
  return (
    <>
      <a className="back" href={up} target={target} onClick={onClick} title="back to the page you came from">← back</a>
      {up !== home && <>{' · '}<a href={home} target={target}>fleet</a></>}
    </>
  );
}

/**
 * The way up from a page in the page viewer: always to the home of the cadre
 * the page belongs to, else the fleet; its tooltip names where ("back to Dev's home"). It
 * climbs; it does not retrace. The browser's own back retraces, which a link
 * here could only guess at: every link followed inside the framed page adds
 * history entries of its own.
 */
export function UpLink({ href, name, target }: { href: string; name: string; target?: string }) {
  // labelled "back" for now (the driver's call), though it climbs to the cadre's home
  return <a className="back" href={href} target={target} title={name ? `back to ${name}'s home` : 'back to the fleet'}>← back</a>;
}
