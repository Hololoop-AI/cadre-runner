import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// On a review page every click lands in the framed page, so this window never
// sees one; the browser still counts it here (navigator.userActivation). The
// chime must start from that, and never before the reader touched anything.
class FakeAudio {
  static made = 0;
  state: 'suspended' | 'running';
  constructor() { FakeAudio.made++; this.state = activated && !strict ? 'running' : 'suspended'; }
  currentTime = 0;
  destination = {};
  async resume() { if (activated && (!strict || clickedHere)) this.state = 'running'; }
}
// every node, parameter and method a Web Audio graph touches: accepts anything, does nothing
const anything: object = new Proxy(function () {}, { get: (_, key) => (key === 'then' ? undefined : anything), apply: () => anything, set: () => true });
for (const make of ['createOscillator', 'createGain', 'createBiquadFilter', 'createDynamicsCompressor']) {
  (FakeAudio.prototype as unknown as Record<string, () => object>)[make] = () => anything;
}
let activated = false;
// a browser that wants a click in this very window: resume() does nothing for a click elsewhere
let strict = false;
let clickedHere = false;
let stops: (() => void)[] = [];

async function chime() {
  vi.resetModules();
  return import('./chime');
}

beforeEach(() => {
  activated = false;
  strict = false;
  clickedHere = false;
  FakeAudio.made = 0;
  vi.stubGlobal('AudioContext', FakeAudio);
  Object.defineProperty(navigator, 'userActivation', { configurable: true, get: () => ({ hasBeenActive: activated, isActive: activated }) });
});
afterEach(() => {
  stops.forEach((stop) => stop());
  stops = [];
  vi.unstubAllGlobals();
  delete (navigator as unknown as Record<string, unknown>).userActivation;
});
const settle = () => new Promise((r) => setTimeout(r, 0));

describe('the chime on a review page, where every click lands in the framed page', () => {
  it('starts when focus moves into the page after a click there', async () => {
    const { unlockChimeOnGesture, playChime } = await chime();
    stops.push(unlockChimeOnGesture());
    expect(playChime()).toBe(false);
    activated = true; // the reader clicked in the framed page...
    window.dispatchEvent(new Event('blur')); // ...which took focus out of this window
    await settle();
    expect(FakeAudio.made).toBe(1);
    expect(playChime()).toBe(true);
  });

  it('a window left before any click (another tab) makes no sound', async () => {
    const { unlockChimeOnGesture, playChime } = await chime();
    stops.push(unlockChimeOnGesture());
    window.dispatchEvent(new Event('blur'));
    expect(FakeAudio.made).toBe(0);
    expect(playChime()).toBe(false);
    expect(FakeAudio.made).toBe(0);
  });

  it('with no focus change, the chime that is due makes the sound itself once the reader has clicked', async () => {
    const { playChime } = await chime();
    activated = true;
    expect(playChime()).toBe(true);
    expect(FakeAudio.made).toBe(1);
  });

  it('a click in this window still unlocks it, as before', async () => {
    const { unlockChimeOnGesture, playChime } = await chime();
    stops.push(unlockChimeOnGesture());
    activated = true;
    document.dispatchEvent(new Event('pointerdown'));
    await settle();
    expect(playChime('problem')).toBe(true);
  });

  it('a browser that wants a click in this window keeps listening for one after a click in the page', async () => {
    const { unlockChimeOnGesture, playChime } = await chime();
    strict = true;
    stops.push(unlockChimeOnGesture());
    activated = true;
    window.dispatchEvent(new Event('blur'));
    await settle();
    expect(playChime()).toBe(false); // suspended: this browser wants a click here
    clickedHere = true;
    document.dispatchEvent(new Event('pointerdown'));
    await settle();
    expect(playChime()).toBe(true);
  });

  it('a chime due while the sound is still suspended asks to resume it, for the next one', async () => {
    const { playChime } = await chime();
    strict = true;
    activated = true;
    expect(playChime()).toBe(false);
    clickedHere = true; // the reader then clicks in this window
    expect(playChime()).toBe(false); // this chime is still too early, but it asks to resume...
    await settle();
    expect(playChime()).toBe(true); // ...so the next one rings
  });
});
