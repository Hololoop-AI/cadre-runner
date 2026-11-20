import { describe, expect, it } from 'vitest';
import { bellStep, chimeId, isUnread, unreadNotifications, type Notification } from './index';

const row = (key: string, at: string, actionable = true, problem = false): Notification =>
  ({ key, type: 'page.version', label: problem ? 'Run failed' : actionable ? 'Your turn' : 'Working on it', at, title: key, url: `/session/${key}`, actionable, problem });
const rows = [row('b', '2026-11-17T10:05:00.000Z'), row('a', '2026-11-17T10:00:00.000Z')];

describe('unread', () => {
  it('keeps only rows newer than the time seen', () => {
    expect(unreadNotifications(rows, '2026-11-17T10:01:00.000Z').map((r) => r.key)).toEqual(['b']);
    expect(isUnread(rows[1], '2026-11-17T10:01:00.000Z')).toBe(false);
  });
  it('counts everything when nothing was seen', () => {
    expect(unreadNotifications(rows, null)).toHaveLength(2);
  });
});

describe('bellStep', () => {
  const b = rows[0], a = rows[1];
  it('rings for an unseen row that needs the reader and was not chimed for, and remembers it', () => {
    expect(bellStep(rows, [chimeId(a)], a.at)).toEqual({ seenAt: a.at, chimed: [chimeId(a), chimeId(b)], ring: 'ready' });
  });
  it('does not ring twice for the same row', () => {
    expect(bellStep(rows, [chimeId(b)], a.at).ring).toBe(false);
  });
  it('does not ring for what the reader already saw', () => {
    expect(bellStep(rows, [], b.at).ring).toBe(false);
  });
  it('rings for a turn timed earlier than a row already chimed for (it came back late)', () => {
    const late = row('c', '2026-11-17T10:03:00.000Z'); // ended before b, but listed after b rang
    expect(bellStep([b, late, a], [chimeId(a), chimeId(b)], a.at)).toMatchObject({ ring: 'ready' });
  });
  it('a failed run rings the problem chime, even beside a ready one', () => {
    const failed = row('f', '2026-11-17T10:04:00.000Z', true, true);
    expect(bellStep([b, failed, a], [chimeId(a)], a.at).ring).toBe('problem');
    expect(bellStep([b, failed, a], [chimeId(a), chimeId(failed)], a.at).ring).toBe('ready');
  });
  it('never rings for a quiet row', () => {
    const quietOnTop = [row('w', '2026-11-17T10:09:00.000Z', false), ...rows];
    expect(bellStep(quietOnTop, [chimeId(a), chimeId(b)], a.at).ring).toBe(false);
    expect(unreadNotifications(quietOnTop, '2026-11-17T10:01:00.000Z').map((r) => r.key)).toEqual(['b']);
  });
  it('a browser that never looked or never chimed takes what is listed as history', () => {
    expect(bellStep(rows, null, null)).toEqual({ seenAt: b.at, chimed: [chimeId(b), chimeId(a)], ring: false });
    expect(bellStep([], null, null).ring).toBe(false);
  });
});
