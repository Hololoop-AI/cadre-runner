// The bell's sound: two soft sine notes made with Web Audio, so there is no
// sound file to ship. Browsers let a page play sound only after the reader
// has touched it once (autoplay policy), so the context is made, or resumed,
// on the first click or key press; before that a chime is silently skipped.

let ctx: AudioContext | null = null;

function context(): AudioContext | null {
  if (ctx) return ctx;
  const Make = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Make) return null;
  try {
    ctx = new Make();
  } catch {
    return null;
  }
  return ctx;
}

/** Make the audio context on the reader's first gesture. Returns the stop function. */
export function unlockChimeOnGesture(doc: Document = document): () => void {
  const unlock = () => {
    void context()?.resume().catch(() => {});
    stop();
  };
  const stop = () => {
    doc.removeEventListener('pointerdown', unlock, true);
    doc.removeEventListener('keydown', unlock, true);
  };
  doc.addEventListener('pointerdown', unlock, true);
  doc.addEventListener('keydown', unlock, true);
  return stop;
}

/**
 * One note of a chime: its pitch (Hz), when it starts and how long it rings
 * (s), its share of the volume, its wave, how fast it swells in (`attack`),
 * a slide to another pitch (`glide`), an FM bell tone (`fm`: a modulator at
 * `ratio` x the pitch, `index` deep, fading with the note) and a vibrato.
 */
// Richer than the two chimes need, on purpose: any candidate from the review
// page's sampler can be pasted into CHIMES as it is.
interface Note {
  freq: number; at: number; ring: number; level?: number; wave?: OscillatorType; attack?: number;
  glide?: { to: number; time: number }; fm?: { ratio: number; index: number }; vibrato?: { rate: number; depth: number };
}

/**
 * The chimes, chosen by the driver on the notification-center review page
 * (19 Nov 2026), where the other candidates can still be played. `ready`:
 * work is ready for you, two soft low sine notes rising a fourth (A4, E5,
 * "low-warm"). `problem`: a run failed, the same two notes falling (E5,
 * A4, "low-falling"), so it reads as "not right" without alarm.
 */
const CHIMES: Record<'ready' | 'problem', Note[]> = {
  ready: [{ freq: 440, at: 0, ring: 0.6 }, { freq: 659.3, at: 0.12, ring: 0.7 }],
  problem: [{ freq: 659.3, at: 0, ring: 0.5 }, { freq: 440, at: 0.14, ring: 0.8 }],
};

/** Play a chime, quiet. False when this tab cannot play sound yet. */
export function playChime(kind: 'ready' | 'problem' = 'ready', volume = 0.08): boolean {
  const c = ctx; // only once a gesture made it: never start audio on our own
  if (!c || c.state !== 'running') return false;
  const t0 = c.currentTime;
  CHIMES[kind].forEach((note) => playNote(c, note, t0, volume));
  return true;
}

/** Web Audio's exponential ramps throw on a target of 0 or less: never ask for one. */
const floor = (v: number) => Math.max(1e-4, v);

function playNote(c: AudioContext, n: Note, t0: number, volume: number) {
  const start = t0 + n.at, end = start + Math.max(0.05, n.ring);
  // the swell and the slide finish before the fade ends, or the envelope folds over itself
  const attack = Math.min(n.attack ?? 0.01, (end - start) / 2);
  const osc = c.createOscillator();
  const gain = c.createGain();
  osc.type = n.wave ?? 'sine';
  osc.frequency.setValueAtTime(n.freq, start);
  if (n.glide) osc.frequency.exponentialRampToValueAtTime(floor(n.glide.to), start + Math.min(n.glide.time, end - start));
  const extras: OscillatorNode[] = [];
  if (n.fm) { // a modulator bending the pitch, fading as a struck bell's overtones do
    const mod = c.createOscillator(), depth = c.createGain();
    mod.frequency.value = n.freq * n.fm.ratio;
    depth.gain.setValueAtTime(floor(n.freq * n.fm.index), start);
    depth.gain.exponentialRampToValueAtTime(1, end);
    mod.connect(depth).connect(osc.frequency);
    extras.push(mod);
  }
  if (n.vibrato) {
    const lfo = c.createOscillator(), depth = c.createGain();
    lfo.frequency.value = n.vibrato.rate;
    depth.gain.value = n.freq * n.vibrato.depth;
    lfo.connect(depth).connect(osc.frequency);
    extras.push(lfo);
  }
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(floor(volume * (n.level ?? 1)), start + attack);
  gain.gain.exponentialRampToValueAtTime(0.0001, end);
  osc.connect(gain).connect(c.destination);
  [osc, ...extras].forEach((o) => { o.start(start); o.stop(end + 0.05); });
}
