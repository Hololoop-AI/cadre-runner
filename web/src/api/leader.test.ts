import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Across tabs: one tab holds the fleet stream and relays it to the others, so a
// browser keeps one connection however many Cadre tabs are open. Each "tab" here
// is a fresh copy of the module, sharing a fake lock manager and channel.
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
const open = () => FakeStream.all.filter((s) => !s.closed);

class FakeChannel {
  static all: FakeChannel[] = [];
  onmessage: ((e: MessageEvent) => void) | null = null;
  closed = false;
  constructor(public name: string) { FakeChannel.all.push(this); }
  postMessage(data: unknown) {
    FakeChannel.all.filter((c) => c !== this && !c.closed && c.name === this.name)
      .forEach((c) => queueMicrotask(() => c.onmessage?.(new MessageEvent('message', { data }))));
  }
  close() { this.closed = true; }
}

/** Exclusive locks by name, granted in the order asked, like navigator.locks. */
function fakeLocks() {
  const held = new Set<string>();
  const waiting: Record<string, (() => void)[]> = {};
  const grant = (name: string) => {
    const next = waiting[name]?.shift();
    if (next) next();
  };
  return {
    request(name: string, fn: () => unknown) {
      return new Promise((resolve) => {
        const run = () => {
          held.add(name);
          Promise.resolve(fn()).then(resolve).finally(() => { held.delete(name); grant(name); });
        };
        if (held.has(name)) (waiting[name] ||= []).push(run);
        else run();
      });
    },
  };
}

async function tab() {
  vi.resetModules();
  return (await import('./index')).subscribe;
}
const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
  FakeStream.all = [];
  FakeChannel.all = [];
  vi.useFakeTimers();
  vi.stubGlobal('EventSource', FakeStream);
  vi.stubGlobal('BroadcastChannel', FakeChannel);
  Object.defineProperty(navigator, 'locks', { configurable: true, value: fakeLocks() });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); Reflect.deleteProperty(navigator, 'locks'); });

describe('subscribe across tabs', () => {
  it('two tabs share one stream: the second hears changes through the first', async () => {
    const a = vi.fn(), b = vi.fn();
    (await tab())(a, () => {}, '/fleet/events');
    await settle();
    (await tab())(b, () => {}, '/fleet/events');
    await settle();
    expect(open()).toHaveLength(1);
    open()[0].fire('change');
    await vi.advanceTimersByTimeAsync(300);
    expect([a.mock.calls.length, b.mock.calls.length]).toEqual([1, 1]);
  });

  it('a tab that joins late is told the stream is live and catches up once', async () => {
    (await tab())(() => {}, () => {}, '/fleet/events');
    await settle();
    open()[0].fire('hello', '{"upstream": true}');
    const live = vi.fn(), change = vi.fn();
    (await tab())(change, live, '/fleet/events');
    await vi.advanceTimersByTimeAsync(300);
    expect(live).toHaveBeenCalledWith('on');
    expect(change).toHaveBeenCalledTimes(1);
  });

  it('the answer to a joining tab goes to it alone: tabs already open do not fetch again', async () => {
    (await tab())(() => {}, () => {}, '/fleet/events');
    await settle();
    open()[0].fire('hello', '{"upstream": true}');
    const b = vi.fn();
    (await tab())(b, () => {}, '/fleet/events');
    await vi.advanceTimersByTimeAsync(300);
    expect(b).toHaveBeenCalledTimes(1);
    (await tab())(() => {}, () => {}, '/fleet/events'); // a third tab opens
    await vi.advanceTimersByTimeAsync(300);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('when the leading tab closes, a waiting tab opens the stream', async () => {
    const stopA = (await tab())(() => {}, () => {}, '/fleet/events');
    await settle();
    const b = vi.fn();
    (await tab())(b, () => {}, '/fleet/events');
    await settle();
    stopA();
    await settle();
    expect(FakeStream.all).toHaveLength(2);
    expect(open()).toHaveLength(1);
    open()[0].fire('change');
    await vi.advanceTimersByTimeAsync(300);
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('a stream down in the leader puts every tab on the slow poll', async () => {
    (await tab())(() => {}, () => {}, '/fleet/events');
    await settle();
    const live = vi.fn();
    (await tab())(() => {}, live, '/fleet/events');
    await settle();
    open()[0].onerror?.();
    await settle();
    expect(live).toHaveBeenCalledWith('off');
  });

  it('different machines elect separately', async () => {
    (await tab())(() => {}, () => {}, 'http://box-1/fleet/events');
    (await tab())(() => {}, () => {}, 'http://box-2/fleet/events');
    await settle();
    expect(open().map((s) => s.url).sort()).toEqual(['http://box-1/fleet/events', 'http://box-2/fleet/events']);
  });
});
