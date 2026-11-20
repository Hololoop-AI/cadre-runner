import { afterEach, describe, expect, it, vi } from 'vitest';
import { underChimeLock } from './bellStore';

// Over plain HTTP there are no Web Locks: tabs told at once decide a random moment apart.
afterEach(() => { vi.useRealTimers(); Reflect.deleteProperty(navigator, 'locks'); });

describe('underChimeLock', () => {
  it('without Web Locks, decides after a random pause of at most 250 ms', async () => {
    Reflect.deleteProperty(navigator, 'locks');
    vi.useFakeTimers();
    const fn = vi.fn();
    const done = underChimeLock('', fn);
    expect(fn).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(250);
    await done;
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('with Web Locks, decides inside the lock', async () => {
    const request = vi.fn(async (_: string, cb: () => unknown) => cb());
    Object.defineProperty(navigator, 'locks', { configurable: true, value: { request } });
    const fn = vi.fn();
    await underChimeLock(':box-2', fn);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe('cadre:notifications-chime:box-2');
  });
});
