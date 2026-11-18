import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DELAY_MS, installTooltips } from './tooltips';

let off: () => void;
beforeEach(() => { vi.useFakeTimers(); document.body.innerHTML = '<button title="open the card">▸</button><span>plain</span><a title="second">x</a>'; off = installTooltips(); });
afterEach(() => { off(); vi.useRealTimers(); });

const tip = () => document.getElementById('ftip')!;
const over = (el: Element, from: Element | null = null) => {
  if (from) from.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: el }));
  el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: from }));
};

describe('fast tooltips', () => {
  it('shows a title after the short delay and hides the browser one meanwhile', () => {
    const b = document.querySelector('button')!;
    over(b);
    expect(tip().hidden).toBe(true);
    vi.advanceTimersByTime(DELAY_MS);
    expect(tip().hidden).toBe(false);
    expect(tip().textContent).toBe('open the card');
    expect(b.hasAttribute('title')).toBe(false);
  });

  it('puts the title back when the pointer leaves, and shows the next one at once', () => {
    const b = document.querySelector('button')!;
    const a = document.querySelector('a')!;
    over(b);
    vi.advanceTimersByTime(DELAY_MS);
    over(a, b);
    expect(b.getAttribute('title')).toBe('open the card');
    expect(tip().textContent).toBe('second');
    a.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: document.querySelector('span') }));
    expect(tip().hidden).toBe(true);
    expect(a.getAttribute('title')).toBe('second');
  });

  it('closes on Escape and on a click', () => {
    const b = document.querySelector('button')!;
    over(b);
    vi.advanceTimersByTime(DELAY_MS);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(tip().hidden).toBe(true);
    expect(b.getAttribute('title')).toBe('open the card');
  });

  it('keeps a title-only button named while its tip is up', () => {
    document.body.insertAdjacentHTML('beforeend', '<button id="bare" title="archive"></button>');
    const b = document.getElementById('bare')!;
    over(b);
    vi.advanceTimersByTime(DELAY_MS);
    expect(b.getAttribute('aria-label')).toBe('archive');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(b.hasAttribute('aria-label')).toBe(false);
  });
});

describe('fast tooltips and accessibility', () => {
  it("keeps an element's own description while its tip is up, and after", () => {
    document.body.insertAdjacentHTML('beforeend', '<button id="described" title="open" aria-describedby="note">x</button><p id="note">n</p>');
    const b = document.getElementById('described')!;
    b.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    vi.advanceTimersByTime(DELAY_MS);
    expect(b.getAttribute('aria-describedby')?.split(' ')).toEqual(['note', 'ftip']);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(b.getAttribute('aria-describedby')).toBe('note');
  });
});
