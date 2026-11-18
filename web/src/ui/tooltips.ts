// Fast tooltips for the whole app. A `title` attribute gets the browser's own
// tooltip, which waits about a second and cannot be told to hurry. This shows
// the same text, from the same attribute, after DELAY_MS instead; moving from
// one titled element to the next shows the next at once. While its tip is up
// an element's title is lifted into data-tip, so the browser's slow tooltip
// does not show as well; it is put back when the tip goes. Nothing in the
// screens changes: they keep writing `title`.

import './tooltips.css';

export const DELAY_MS = 300;
/** After a tip hides, the next one within this long shows without waiting. */
const WARM_MS = 400;
const GAP = 6;

/** Show every `title` in `doc` as the app's fast tooltip; returns what removes it again. */
export function installTooltips(doc: Document = document): () => void {
  const tip = doc.createElement('div');
  tip.className = 'ftip';
  tip.id = 'ftip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  doc.body.appendChild(tip);

  let owner: HTMLElement | null = null;
  let timer = 0;
  let warmUntil = 0;

  // A screen that sets a new title while its tip is up (a re-render) updates the tip, and the new
  // title is the one put back.
  const watch = new MutationObserver(() => {
    if (!owner?.hasAttribute('title')) return;
    owner.dataset.tip = owner.getAttribute('title') || '';
    owner.removeAttribute('title');
    tip.textContent = owner.dataset.tip;
  });

  const describedBy = (el: HTMLElement) => (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
  const lift = (el: HTMLElement) => {
    const text = el.getAttribute('title') || '';
    el.dataset.tip = text;
    el.removeAttribute('title');
    // the title may have been the element's only name: keep it named while the tip is up
    if (!el.hasAttribute('aria-label') && !(el.textContent || '').trim()) {
      el.setAttribute('aria-label', text);
      el.dataset.tipLabel = text;
    }
    // added to whatever describes it already, and taken away alone
    el.setAttribute('aria-describedby', [...describedBy(el).filter((id) => id !== tip.id), tip.id].join(' '));
    watch.observe(el, { attributes: true, attributeFilter: ['title'] });
    return text;
  };
  const putBack = (el: HTMLElement) => {
    watch.disconnect();
    if (el.dataset.tip !== undefined && !el.hasAttribute('title')) el.setAttribute('title', el.dataset.tip);
    // only the name the tip gave it: one the screen set since stays
    if (el.dataset.tipLabel !== undefined && el.getAttribute('aria-label') === el.dataset.tipLabel) el.removeAttribute('aria-label');
    delete el.dataset.tip;
    delete el.dataset.tipLabel;
    const rest = describedBy(el).filter((id) => id !== tip.id);
    if (rest.length) el.setAttribute('aria-describedby', rest.join(' '));
    else el.removeAttribute('aria-describedby');
  };

  const place = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const vw = doc.documentElement.clientWidth || window.innerWidth;
    const vh = doc.documentElement.clientHeight || window.innerHeight;
    const left = Math.max(GAP, Math.min(r.left + r.width / 2 - w / 2, vw - w - GAP));
    const below = r.bottom + GAP;
    const top = below + h > vh - GAP && r.top - GAP - h > GAP ? r.top - GAP - h : below;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  };

  const hide = () => {
    window.clearTimeout(timer);
    if (owner) {
      putBack(owner);
      owner = null;
      warmUntil = Date.now() + WARM_MS;
    }
    tip.hidden = true;
  };

  const show = (el: HTMLElement) => {
    const text = lift(el);
    owner = el;
    tip.textContent = text;
    tip.hidden = false;
    place(el);
  };

  const arm = (el: HTMLElement) => {
    if (el === owner) return;
    hide();
    if (!(el.getAttribute('title') || '').trim()) return;
    if (Date.now() < warmUntil) show(el);
    else timer = window.setTimeout(() => { if (el.isConnected) show(el); }, DELAY_MS);
  };

  const titled = (t: EventTarget | null) =>
    (t instanceof Element ? t.closest<HTMLElement>('[title], [data-tip]') : null);

  const onOver = (e: Event) => {
    const el = titled(e.target);
    if (el) arm(el);
    else hide();
  };
  const onOut = (e: MouseEvent) => {
    const to = titled(e.relatedTarget);
    if (!(owner || timer) || to === owner) return;
    if (to) arm(to);
    else hide();
  };
  const onFocus = (e: FocusEvent) => {
    const el = titled(e.target);
    if (el && el.matches(':focus-visible')) arm(el);
  };
  const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') hide(); };

  doc.addEventListener('mouseover', onOver);
  doc.addEventListener('mouseout', onOut);
  doc.addEventListener('focusin', onFocus);
  doc.addEventListener('focusout', hide);
  doc.addEventListener('pointerdown', hide, true);
  doc.addEventListener('keydown', onKey);
  window.addEventListener('scroll', hide, true);
  return () => {
    hide();
    doc.removeEventListener('mouseover', onOver);
    doc.removeEventListener('mouseout', onOut);
    doc.removeEventListener('focusin', onFocus);
    doc.removeEventListener('focusout', hide);
    doc.removeEventListener('pointerdown', hide, true);
    doc.removeEventListener('keydown', onKey);
    window.removeEventListener('scroll', hide, true);
    watch.disconnect();
    tip.remove();
  };
}
