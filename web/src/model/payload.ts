// GET /api/fleet, typed as the home page reads it (statusd.fleet_payload).
// Another machine's payload may be version-skewed, so the model reads every
// field defensively, the way statusd.py does.

/** A review page the runner registered (status.json surfaces), as collapse() leaves it. */
export interface Surface {
  kind?: string | null;
  path?: string | null;
  /** epoch seconds */
  opened?: number | null;
  project?: string | null;
  title?: string | null;
  task?: string | null;
  cwd?: string | null;
  artifact?: string | null;
  stranded?: string[] | null;
  ended?: boolean;
  /** added by collapse() */
  earlier?: Earlier[];
  page_title?: string;
  [k: string]: unknown;
}

/** An older round of a conversation, folded under its newest page. */
export interface Earlier {
  key: string;
  path: string;
  title: string;
  opened: number;
}

/** A page's live state from the page server's /api/<key>/agent-status. */
export interface SessionStatus {
  status?: string | null;
  presence?: string | null;
  pending_prompts?: number | null;
  updated_at?: string | null;
  last_agent_reply_at?: string | null;
  viewed_at?: string | null;
  /** null when the page server does not know */
  unseen?: boolean | null;
}

/** The page server's record of a page: its file, title, last update, whether it ended. */
export interface PageInfo {
  file?: string;
  title?: string;
  updated?: number;
  ended?: boolean;
}

/** A link between two pages: one replaces the other, carries it on, or files under it. */
export interface LinkRecord {
  type: string;
  from: string;
  to: string;
}

/** A durable project (projects.json). */
export interface Project {
  id: string;
  name: string;
  dirs?: string[];
  group?: string | null;
  archived?: boolean;
}

/** A session in the launch tree (session_tree.load): what it is and who started it. */
export interface TreeNode {
  title?: string;
  project?: string;
  since?: number;
  parent?: string;
  working?: boolean;
  node?: string;
  round?: string;
  result?: string;
  page?: string;
  [k: string]: unknown;
}

/** One agent turn running now, or waiting for a slot (statusd.running_agents). */
export interface RunningCard {
  task: string;
  title?: string;
  doing?: string;
  agents?: number;
  page?: string;
  page_title?: string;
  item?: string;
  item_title?: string;
  folder?: string;
  started?: number;
  deadline?: number;
  waiting?: boolean;
}

/** A launched task with no page yet (statusd.pageless_tasks). */
export interface Pageless {
  task: string;
  since?: number;
  project?: string;
  title?: string;
  cwd?: string;
  running?: boolean;
  ran?: boolean;
  started?: boolean;
  failed?: string;
}

/** A run that ended without leaving a page (statusd.bare_endings). */
export interface Ending {
  ended?: number;
  ok?: boolean;
  node?: string;
}

/** An item's tags, done and hidden marks (items.json). */
export interface ItemRecord {
  tags?: string[];
  done_at?: number | null;
  hidden_at?: number | null;
}

/** GET /api/fleet: everything the home page is drawn from, for one machine. */
export interface Fleet {
  version?: string;
  instance?: string;
  machine?: string;
  review_events?: boolean;
  snap?: {
    surfaces?: Surface[];
    iso?: string;
    /** the daemon's code stamp: what it runs vs what the folder holds */
    code?: { running?: string; tree?: string; since?: number; head?: string };
  };
  /** [link records, holders, pages by key] */
  graph?: [LinkRecord[], Record<string, string>, Record<string, PageInfo>];
  statuses?: Record<string, SessionStatus>;
  projs?: Record<string, Project>;
  doing?: Record<string, string>;
  spent?: Record<string, number>;
  returned?: Record<string, number>;
  week_usd?: number;
  histories?: Record<string, number>;
  tree?: Record<string, TreeNode>;
  running?: RunningCard[];
  slots?: number;
  pageless?: Pageless[];
  endings?: Record<string, Ending>;
  items?: Record<string, ItemRecord>;
  fleet_page_code?: { stale?: boolean; since?: number; head?: string };
}

/** One machine the app can switch to: its name and origin ('' = where the app was loaded from). */
export interface Backend {
  name: string;
  url: string;
}

/** GET /api/backends: this machine first, then the configured others. */
export interface Backends {
  backends: Backend[];
}

/** GET /fs: directories on the server's machine completing what was typed. */
export interface Dirs {
  dirs: string[];
}

/** GET /api/newwork */
export interface NewWorkInfo {
  home: string;
  library: LibraryEntry[];
}

/** One installed skill, agent, workflow or flow the New work picker offers. */
export interface LibraryEntry {
  /** kind:name, what the form posts as `run` */
  v: string;
  /** how it is invoked */
  i: string;
  k: string;
  d: string;
}
