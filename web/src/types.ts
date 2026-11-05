// The parts of GET /api/fleet this screen reads. Fields are typed from live
// data; anything the screen does not use is left out on purpose.

export interface RunningCard {
  task: string;
  title: string;
  doing: string;
  agents?: number;
  page: string;
  page_title?: string;
  item?: string;
  item_title: string;
  folder: string;
  /** epoch seconds */
  started: number;
  /** epoch seconds, 0 when none */
  deadline: number;
  /** true = asked for but not yet given a slot */
  waiting: boolean;
}

export interface Surface {
  kind: string;
  path: string;
  /** epoch seconds */
  opened: number;
  project: string | null;
  title: string | null;
  task: string | null;
  cwd: string | null;
  artifact: string | null;
}

export interface SessionStatus {
  status: 'open' | 'ended' | string;
  presence: 'waiting' | 'working' | string | null;
  pending_prompts: number;
  updated_at: string | null;
  last_agent_reply_at: string | null;
  viewed_at: string | null;
  /** null when review-surface does not know */
  unseen: boolean | null;
}

export interface RecentPage {
  path: string;
  key: string;
  open: boolean;
  title: string | null;
}

export interface RecentTurn {
  task: string;
  project: string;
  node: string;
  /** epoch seconds */
  ended: number;
  seconds: number;
  cost: number;
  ok: boolean;
  turns: number;
  page: RecentPage | null;
  open: boolean;
}

export interface Project {
  id: string;
  name: string;
  dirs: string[];
  group: string | null;
  archived: boolean;
}

export interface GraphRecord {
  type: 'supersedes' | 'derived-from' | string;
  from: string;
  to: string;
}

export interface PageInfo {
  title?: string;
  updated?: number;
  ended?: boolean;
}

export interface ItemState {
  tags: string[];
  done_at: number | null;
  hidden_at: number | null;
}

export interface Fleet {
  machine: string;
  week_usd: number;
  slots: number;
  snap: { surfaces: Surface[] };
  /** [records, holders, pages-by-key] */
  graph: [GraphRecord[], Record<string, unknown>, Record<string, PageInfo>];
  statuses: Record<string, SessionStatus>;
  recent: RecentTurn[];
  projs: Record<string, Project>;
  running: RunningCard[];
  items: Record<string, ItemState>;
  tree?: Record<string, { title?: string }>;
}
