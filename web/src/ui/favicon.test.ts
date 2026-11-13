import { describe, expect, it } from 'vitest';
import { followThemeFavicon, themeFavicon } from './favicon';

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

describe('followThemeFavicon', () => {
  it('redraws the tab icon by itself when the theme changes, and stops when told', async () => {
    document.head.innerHTML = '<link rel="icon" href="data:,">';
    document.documentElement.style.setProperty('--accent', '#111111');
    const stop = followThemeFavicon();
    const href = () => decodeURIComponent(document.querySelector<HTMLLinkElement>('link[rel~=icon]')!.href);
    expect(href()).toContain("stroke='#111111'");
    document.documentElement.style.setProperty('--accent', '#222222');
    await Promise.resolve(); // mutation observers run as a microtask
    expect(href()).toContain("stroke='#222222'");
    stop();
    document.documentElement.style.setProperty('--accent', '#333333');
    await Promise.resolve();
    expect(href()).toContain("stroke='#222222'");
  });
});
