// Pure data shaping: GET /api/fleet in, the rows each section draws out.
import type { Fleet, RunningCard, SessionStatus, Surface } from './types';

export interface NeedsRow {
  key: string;
  path: string;
  title: string;
  project: string;
  task: string | null;
  /** the agent answered and the driver has not opened the page since */
  unseen: boolean;
  /** the agent has replied at least once on this page */
  replied: boolean;
  /** epoch seconds: the newest sign of life on the page */
  since: number;
}

export interface RecentRow {
  task: string;
  title: string;
  project: string;
  path: string | null;
  ended: number;
  seconds: number;
  cost: number;
  ok: boolean;
}

/** The session key is the last path segment: /session/<key>. */
export function keyOf(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts[parts.length - 1] ?? '';
}

function isoEpoch(iso: string | null | undefined): number {
  if (!iso) return 0;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t / 1000;
}

/**
 * Is the ball in the driver's court on this page? Mirrors statusd.py's
 * agent_state_badge: an open session whose agent is not working and has no
 * feedback queued for it ("awaiting you" / "agent replied — your turn").
 * Pending prompts mean the driver already answered and the agent owes the
 * next move; "ended" means nothing more will happen there.
 */
export function isDriversTurn(st: SessionStatus | undefined): boolean {
  if (!st) return false;
  if (st.status !== 'open') return false;
  if (st.presence !== 'waiting') return false;
  return Number(st.pending_prompts || 0) === 0;
}

export function needsYou(fleet: Fleet): NeedsRow[] {
  const [records = [], , pages = {}] = fleet.graph ?? [];
  // "A supersedes B": B is an older round; only the newest round asks anything.
  const superseded = new Set(records.filter((r) => r.type === 'supersedes').map((r) => r.to));
  const busy = new Set((fleet.running ?? []).filter((c) => !c.waiting).map((c) => c.task));
  const items = fleet.items ?? {};

  const byTask = new Map<string, NeedsRow>();
  const loose: NeedsRow[] = [];
  for (const sf of fleet.snap?.surfaces ?? []) {
    const key = keyOf(sf.path);
    if (!key || superseded.has(key)) continue;
    const st = fleet.statuses?.[key];
    if (!isDriversTurn(st)) continue;
    const title = titleOf(sf, pages[key]?.title);
    // A title carrying DECIDED is settled (statusd.py's interim marker).
    if (title.includes('DECIDED')) continue;
    if (sf.task) {
      if (busy.has(sf.task)) continue; // an agent is on it right now
      const item = items[sf.task];
      if (item && (item.done_at || item.hidden_at)) continue;
    }
    const row: NeedsRow = {
      key,
      path: sf.path,
      title,
      project: sf.project || '',
      task: sf.task,
      unseen: st!.unseen === true,
      replied: Boolean(st!.last_agent_reply_at),
      since: Math.max(sf.opened || 0, isoEpoch(st!.last_agent_reply_at), isoEpoch(st!.updated_at)),
    };
    if (!sf.task) {
      loose.push(row);
      continue;
    }
    // one row per task: its newest page
    const prev = byTask.get(sf.task);
    if (!prev || row.since > prev.since) byTask.set(sf.task, row);
  }
  return [...byTask.values(), ...loose].sort(
    (a, b) => Number(b.unseen) - Number(a.unseen) || b.since - a.since,
  );
}

function titleOf(sf: Surface, pageTitle: string | undefined): string {
  return sf.title || pageTitle || sf.task || sf.path;
}

export function running(fleet: Fleet): RunningCard[] {
  return [...(fleet.running ?? [])].sort((a, b) => b.started - a.started);
}

export function recentlyFinished(fleet: Fleet): RecentRow[] {
  return (fleet.recent ?? []).map((r) => ({
    task: r.task,
    title: r.page?.title || fleet.tree?.[r.task]?.title || r.task,
    project: fleet.projs?.[r.project]?.name || r.project,
    path: r.page?.path || null,
    ended: r.ended,
    seconds: r.seconds,
    cost: r.cost,
    ok: r.ok,
  }));
}

export function elapsed(fromEpoch: number, nowMs: number): string {
  const s = Math.max(0, Math.floor(nowMs / 1000 - fromEpoch));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function ago(epoch: number, nowMs: number): string {
  return `${elapsed(epoch, nowMs)} ago`;
}

export function usd(n: number): string {
  return `$${(n || 0).toFixed(2)}`;
}
