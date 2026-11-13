import { describe, expect, it } from 'vitest';
import { themeFavicon } from './GroupIcon';

// The tab icon is drawn in whatever --accent the page has when it is called,
// so a theme switch that calls it again recolours the tab.
describe('themeFavicon', () => {
  it('draws the Hololoop mark in the current --accent, and again after it changes', () => {
    document.head.innerHTML = '<link rel="icon" href="data:,">';
    document.documentElement.style.setProperty('--accent', '#123456');
    themeFavicon();
    const link = document.querySelector<HTMLLinkElement>('link[rel~=icon]')!;
    expect(decodeURIComponent(link.href)).toContain("stroke='#123456'");
    document.documentElement.style.setProperty('--accent', '#abcdef');
    themeFavicon();
    expect(decodeURIComponent(link.href)).toContain("stroke='#abcdef'");
    expect(document.querySelectorAll('link[rel~=icon]').length).toBe(1);
  });
});
