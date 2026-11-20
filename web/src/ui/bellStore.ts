// Where the bell keeps its read state: this origin's localStorage. The seen
// and sound keys keep the names the page server's own bell used before it was
// removed, so what a reader had already seen carries over. A Web Lock shared
// by every tab of the origin makes two tabs told of the same change decide one
// at a time, so one of them chimes.

/** The newest row time the reader has seen. */
export const SEEN_KEY = 'review-surface:notifications-seen-at';
/** The rows some tab chimed for (page@time, JSON), so a row rings once across tabs. */
export const CHIMED_KEY = 'cadre:notifications-chimed';
/** 'off' when the reader muted the chime, for every tab. */
export const SOUND_KEY = 'review-surface:notifications-sound';
const CHIME_LOCK = 'cadre:notifications-chime';

export function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function write(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // storage unavailable: the bell forgets across reloads
  }
}

/** Run `fn` holding the chime lock (`suffix` names the machine). */
export async function underChimeLock(suffix: string, fn: () => void) {
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  if (locks) return void (await locks.request(CHIME_LOCK + suffix, async () => fn()));
  // Web Locks exist only on HTTPS or localhost; over plain HTTP (a fleet reached by its
  // tailnet name) tabs told of the same change wait a random moment first, so the one that
  // goes second reads the chime the first one recorded, and does not ring again
  await new Promise((done) => setTimeout(done, Math.random() * 250));
  fn();
}

/** The chimed-for rows; null before this browser ever chimed (or from the older one-time format). */
export function readChimed(key: string): string[] | null {
  try {
    const list = JSON.parse(read(key) ?? 'null');
    return Array.isArray(list) ? list.map(String) : null;
  } catch {
    return null;
  }
}
