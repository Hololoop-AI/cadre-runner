// The tab icon: the Hololoop mark in the theme's accent. A favicon is a
// separate image that cannot read the page's CSS, so it is drawn from the
// page's --accent and redrawn whenever the theme could have changed.

/** The theme's accent as the page has it now; fleet.css's when the page has none. */
function accentOf(doc: Document): string {
  return getComputedStyle(doc.documentElement).getPropertyValue('--accent').trim() || '#7fbbb3';
}

/** Set the tab icon to the Hololoop mark in `accent` (the page's --accent unless given). */
export function themeFavicon(doc: Document = document, accent: string = accentOf(doc)) {
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
  let last: string | null = null;
  const draw = () => {
    const accent = accentOf(doc);
    if (accent === last) return; // our own icon swap lands in the head too
    last = accent;
    themeFavicon(doc, accent);
  };
  draw();
  const watch = new MutationObserver(draw);
  watch.observe(doc.documentElement, { attributes: true, attributeFilter: ['style', 'class', 'data-theme'] });
  watch.observe(doc.head, { childList: true, subtree: true }); // a stylesheet added, swapped or rewritten
  return () => watch.disconnect();
}
