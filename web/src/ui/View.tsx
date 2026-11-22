// The frame around one surface (/view/<key>): a bar with the way up to the
// page's cadre and the page's title, and the agent's own page filling the rest of the
// window in an iframe. Study starts from a thread on the page itself (select a
// passage, then Study), so the bar carries no study box of its own.
//
// The page is the chosen machine's /session/<key>, so the frame works
// whichever machine the app is looking at, and from an app shell that is not
// served by any of them: the page server lets this machine's fleet and the
// configured machines' apps frame it (statusd.framable_header). The page's
// annotation layer is the page server's own script inside the frame; the
// frame never reaches into it beyond reading its title.

import { useEffect, useRef, useState } from 'react';
import { noticeOf } from '../api';
import { MOUNT } from './router';
import { useOnce } from './Screen';
import { useServer } from './server';
import './view.css';
import { UpLink } from './Back';
import { cadreHref } from './Cadre';
import { Bell } from './Bell';

const SESSION = '/session/';

export function ViewScreen({ pageKey }: { pageKey: string }) {
  const server = useServer();
  const { data } = useOnce(() => server.fetchView(pageKey), pageKey);
  // the surface shown now: a link inside the page can move the frame to another one
  const [key, setKey] = useState(pageKey);
  const [frameTitle, setFrameTitle] = useState('');
  const [notice] = useState(() => noticeOf(window.location.href));
  const frame = useRef<HTMLIFrameElement>(null);
  const title = frameTitle || (key === pageKey ? data?.title : '') || '';

  useEffect(() => {
    document.title = (title || 'Surface') + ' — Cadre';
  }, [title]);

  // Same origin (this machine's page), the bar reads the page's title and
  // follows the frame to another surface. Another machine's page is
  // cross-origin: reading it throws, and the bar keeps what /api/view said.
  const onLoad = () => {
    try {
      const w = frame.current?.contentWindow;
      const d = w?.document;
      if (d?.title) setFrameTitle(d.title);
      const p = w?.location.pathname || '';
      if (p.startsWith(SESSION) && p.length > SESSION.length) {
        const next = p.slice(SESSION.length);
        setKey(next);
        // absolute: another machine's <base> would re-aim a relative URL
        // the same page keeps its deep link (query, #section); another page gets a clean address
        const same = window.location.pathname.endsWith('/view/' + next);
        history.replaceState(null, '', window.location.origin + MOUNT + '/view/' + next
          + (same ? window.location.search + window.location.hash : ''));
      }
    } catch {
      /* another machine's page: nothing to read */
    }
  };

  // a deep link's query and #section reach the page, as they would have at /session/<key>
  const deep = new URLSearchParams(window.location.search);
  deep.delete('notice'); // the frame's own
  const src = server.url(SESSION + encodeURIComponent(pageKey) + (deep.size ? '?' + deep : '') + window.location.hash);
  return (
    <div className="view">
      <nav className="viewbar">
        <span><UpLink href={window.location.origin + MOUNT + (data?.cadre ? cadreHref(data.cadre) : '/')} name={data?.cadre ? data.cadre_name || '' : ''} target="_self" /></span>
        <span className="title">{title}</span>
        {notice && <span className="notice">{notice}</span>}
        <Bell />
      </nav>
      {/* named for screen readers by aria-label, not title: the app shows every title as a
          tooltip (tooltips.ts), which put a stray "surface" tip over the page */}
      <iframe ref={frame} src={src} aria-label={title || 'Review page'} onLoad={onLoad} />
    </div>
  );
}
