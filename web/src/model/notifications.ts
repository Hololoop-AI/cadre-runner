// The bell's rules: which rows are new, and what one fetch changes about what
// was seen and chimed for.
// Pure; the times are the ISO strings Cadre's notification center sends
// (runnerlib/notifications.py), so they compare as text.
import type { Notification } from './payload';

/** Before anything is listed: the start of time, so the first row is news. */
const EPOCH = '1970-01-01T00:00:00.000Z';

/** Is this row newer than the newest time the reader has seen ('' or null = none seen)? */
export function isUnread(row: Notification, seenAt: string | null): boolean {
  return row.at > (seenAt || '');
}

/** The rows that need the reader and that they have not seen: what the badge counts. */
export function unreadNotifications(rows: Notification[], seenAt: string | null): Notification[] {
  return rows.filter((row) => row.actionable && isUnread(row, seenAt));
}

/** How a row is remembered as chimed for: its page and the time it says. */
export function chimeId(row: Notification): string {
  return row.key + '@' + row.at;
}

/** How many chimed-for rows are remembered: more than the list ever holds. */
const CHIMED_KEPT = 100;

/**
 * What one fetch of `rows` (newest first) does to the browser's read state.
 * A browser that never looked or never chimed takes everything already
 * listed as history: no count, no chime. Otherwise it rings for a row that
 * needs the reader (`actionable`; the rest are listed quietly), is unseen,
 * and was not chimed for before; `ring` says which chime, the problem one
 * when a new row is a failed run. Rows are remembered by page and time, not
 * by one newest time, because a turn's "Your turn" is timed when the turn
 * ended, which can be earlier than a row already chimed for. The caller
 * records `chimed` only once a chime played (or the reader muted it), so a
 * tab that cannot play sound leaves the chime to one that can.
 */
export function bellStep(rows: Notification[], chimed: string[] | null, seenAt: string | null):
  { seenAt: string; chimed: string[]; ring: false | 'ready' | 'problem' } {
  const loud = rows.filter((row) => row.actionable);
  const ids = loud.map(chimeId);
  if (seenAt === null || chimed === null) {
    return { seenAt: seenAt ?? (rows[0]?.at || EPOCH), chimed: [...new Set([...(chimed ?? []), ...ids])].slice(-CHIMED_KEPT), ring: false };
  }
  const fresh = loud.filter((row) => isUnread(row, seenAt) && !chimed.includes(chimeId(row)));
  // one chime per fetch: the problem chime when any new row is a failed run
  const ring = !fresh.length ? false : fresh.some((row) => row.problem) ? 'problem' : 'ready';
  return { seenAt, chimed: fresh.length ? [...chimed, ...fresh.map(chimeId)].slice(-CHIMED_KEPT) : chimed, ring };
}
