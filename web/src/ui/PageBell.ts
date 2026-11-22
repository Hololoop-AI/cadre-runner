// The bell in the framed review page's own bar, where the page's bell used to
// be, while the frame's bar keeps only the way back and the title. The page
// offers a bell slot (`review-surface:barHello`); the frame answers
// `review-surface-host:carryBell` and sends what the bell shows (`bellState`);
// a click on it (`review-surface:bellClick`, with the button's box) opens
// Cadre's own menu under it, and Escape in the page (`review-surface:bellClose`)
// closes it. Review Surface's README, "Framing hosts", owns the messages.
// Messages only: another machine's page is cross-origin. A page that loads
// without offering (an older page server) gets the bell back in the frame's bar.

import { useEffect, useRef, useState, type RefObject } from 'react';
import { useBellBadge, type BellHandle } from './Bell';

/** The gap between the page's bell and the menu under it, as the page's own menus leave. */
const GAP = 6;
/** The least room kept between the menu and the window's edges, whatever the page reports. */
const EDGE = 12;

export function usePageBell(frame: RefObject<HTMLIFrameElement | null>) {
  // waiting: no bell in the frame's bar until the page offers, or loads without offering
  const [mode, setMode] = useState<'waiting' | 'page' | 'frame'>('waiting');
  const [open, setOpen] = useState(false);
  const bell = useRef<BellHandle>(null);
  // the origin the page offered from; null until it has, and messages go only there
  const [origin, setOrigin] = useState<string | null>(null);
  const originNow = useRef<string | null>(null);
  // a page that offers does so before its load event
  const offeredSinceLoad = useRef(false);
  // each offer: a reloaded page gets the bell's state again
  const [offers, setOffers] = useState(0);
  const badge = useBellBadge();

  useEffect(() => {
    const heard = (e: MessageEvent) => {
      const iframe = frame.current;
      const page = iframe?.contentWindow;
      if (!iframe || !page || e.source !== page) return;
      const m = e.data || {};
      if (m.type === 'review-surface:barHello') {
        // a page with no origin of its own cannot be answered at a known address
        if (!e.origin || e.origin === 'null') return;
        originNow.current = e.origin;
        setOrigin(e.origin);
        offeredSinceLoad.current = true;
        setMode('page');
        setOffers((n) => n + 1);
        page.postMessage({ type: 'review-surface-host:carryBell' }, e.origin);
      } else if (originNow.current === null) {
        return;
      } else if (m.type === 'review-surface:bellClick') {
        const box = iframe.getBoundingClientRect();
        const bottom = Number(m.rect?.bottom), right = Number(m.rect?.right);
        if (!Number.isFinite(bottom) || !Number.isFinite(right)) return;
        const clamp = (v: number, most: number) => Math.min(Math.max(v, EDGE), Math.max(EDGE, most));
        bell.current?.toggle({
          top: clamp(box.top + bottom + GAP, window.innerHeight - EDGE),
          right: clamp(window.innerWidth - (box.left + right), window.innerWidth - EDGE),
        });
      } else if (m.type === 'review-surface:bellClose') {
        bell.current?.close();
      }
    };
    window.addEventListener('message', heard);
    return () => window.removeEventListener('message', heard);
  }, [frame]);

  // what the page's bell shows: sent on every change, and again to a page that offers anew
  const { count, problem, label } = badge;
  useEffect(() => {
    if (origin === null || mode !== 'page') return;
    frame.current?.contentWindow?.postMessage(
      { type: 'review-surface-host:bellState', count, problem, open, label }, origin);
  }, [frame, origin, mode, offers, count, problem, open, label]);

  const frameLoaded = () => {
    if (!offeredSinceLoad.current) {
      originNow.current = null;
      setOrigin(null);
      setMode('frame');
    }
    offeredSinceLoad.current = false;
  };

  return { bell: { button: mode === 'frame', handle: bell, onOpenChange: setOpen }, frameLoaded };
}
