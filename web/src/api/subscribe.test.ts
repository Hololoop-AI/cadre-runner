import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { subscribe } from './index';

// Every subscriber in a tab shares one fleet stream per URL (the screen and the bell).
class FakeStream {
  static all: FakeStream[] = [];
  handlers: Record<string, ((e: Event) => void)[]> = {};
  onerror: (() => void) | null = null;
  readyState = 0;
  closed = false;
  constructor(public url: string) { FakeStream.all.push(this); }
  addEventListener(name: string, fn: (e: Event) => void) { (this.handlers[name] ||= []).push(fn); }
  close() { this.closed = true; this.readyState = 2; }
  fire(name: string, data = '{}') { (this.handlers[name] || []).forEach((fn) => fn(new MessageEvent(name, { data }))); }
}

beforeEach(() => { FakeStream.all = []; vi.useFakeTimers(); vi.stubGlobal('EventSource', FakeStream); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('subscribe', () => {
  it('shares one stream, tells each subscriber of a change, and closes with the last one', () => {
    const a = vi.fn(), b = vi.fn();
    const stopA = subscribe(a, () => {}, '/s1');
    const stopB = subscribe(b, () => {}, '/s1');
    expect(FakeStream.all).toHaveLength(1);
    FakeStream.all[0].fire('change');
    vi.advanceTimersByTime(300);
    expect([a.mock.calls.length, b.mock.calls.length]).toEqual([1, 1]);
    stopA();
    expect(FakeStream.all[0].closed).toBe(false);
    stopB();
    expect(FakeStream.all[0].closed).toBe(true);
  });

  it('a late subscriber is told the stream is live and catches up once', () => {
    subscribe(() => {}, () => {}, '/s2');
    FakeStream.all[0].fire('hello', '{"upstream": true}');
    const live = vi.fn(), change = vi.fn();
    subscribe(change, live, '/s2');
    expect(live).toHaveBeenCalledWith('on');
    vi.advanceTimersByTime(300);
    expect(change).toHaveBeenCalledTimes(1);
  });

  it('a second stop is harmless: it does not drop a newer stream for the same URL', () => {
    const stop = subscribe(() => {}, () => {}, '/s3');
    stop();
    const keep = vi.fn();
    subscribe(keep, () => {}, '/s3');
    stop(); // again, after a new stream opened
    FakeStream.all[1].fire('change');
    vi.advanceTimersByTime(300);
    expect(keep).toHaveBeenCalledTimes(1);
    subscribe(() => {}, () => {}, '/s3');
    expect(FakeStream.all).toHaveLength(2); // still shared, not a third
  });

  it('a stream the browser closed for good is opened again after a pause', () => {
    const live = vi.fn();
    subscribe(() => {}, live, '/s4');
    FakeStream.all[0].readyState = 2;
    FakeStream.all[0].onerror?.();
    expect(live).toHaveBeenCalledWith('off');
    vi.advanceTimersByTime(5000);
    expect(FakeStream.all).toHaveLength(2);
  });
});
