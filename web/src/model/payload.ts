// GET /api/fleet, typed as the home page reads it (statusd.fleet_payload).
// Another machine's payload may be version-skewed, so the model reads every
// field defensively, the way statusd.py does.

/** A review page the runner registered (status.json surfaces), as collapse() leaves it. */
export interface Surface {
  kind?: string | null;
  /** "orchestrator" for a terminal session that runs others, else a free label */
  role?: string | null;
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
  /** a folder holding brief.md and decisions/ */
  context?: string;
  /** skills every task launched here is told to use */
  skills?: string[];
  /** the kind:name a launch here runs unless another is picked */
  launch?: string;
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
  /** the session that launched this one, when this machine does not know it as a task */
  launcher?: string;
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

/** One installed skill, workflow, agent or flow (library.scan), as /api/library lists it. */
export interface InstalledEntry {
  kind: string;
  name: string;
  invoke: string;
  scope: string;
  description: string;
  broken: boolean;
}

/** GET /api/library: everything a launch can pick, and the project folders scanned. */
export interface Library {
  entries: InstalledEntry[];
  scanned: string[];
}

/** One way of cutting spend (costs._bucket): by node, by project or by task. */
export interface CostBucket {
  key: string;
  usd: number;
  runs: number;
  unpriced?: number;
  /** by task only: the task's project */
  project?: string;
}

/** One finished turn as the runner logged it (history.jsonl). */
export interface TurnRecord {
  story?: string | null;
  stage?: string | null;
  model?: string | null;
  seconds?: number | null;
  ended?: number | null;
  ok?: boolean | null;
  cost_usd?: number | null;
}

/** GET /api/costs (and /costs.json): costs.summarize over every finished turn. */
export interface CostSummary {
  total_usd: number;
  priced: number;
  unpriced: number;
  last_24h_usd: number;
  last_7d_usd: number;
  by_node: CostBucket[];
  by_project: CostBucket[];
  by_task: CostBucket[];
  /** the newest turns, newest first */
  recent: TurnRecord[];
}

/** One row of the runs screen (recent.rows): a task's run, or one turn. */
export interface RunRow {
  task: string;
  project: string;
  node: string;
  ended: number;
  seconds: number;
  cost: number | null;
  ok: boolean;
  turns: number;
  /** the task's newest page, enough to open and manage it */
  page: { path?: string; key?: string; title?: string } | null;
  open: boolean;
}

/** GET /api/runs?view=&sort=&project=&node=&q=: one view of every run. */
export interface Runs {
  /** at most `limit` rows */
  rows: RunRow[];
  /** how many rows the view has */
  total: number;
  limit: number;
  /** every project and node a filter can pick */
  projects: string[];
  nodes: string[];
}

/** GET /api/tree: every session that launched or was launched, by task. */
export interface TreePayload {
  nodes: Record<string, TreeNode>;
}

/** A page of a project that has ended (statusd.closed_pages). */
export interface ClosedPage {
  key: string;
  title?: string | null;
  task?: string | null;
  opened?: number | null;
}

/** A project's context store: where it is, its brief, its decision files. */
export interface ProjectContext {
  path: string;
  /** the brief's first 4000 characters, "" when there is none */
  brief: string;
  /** decision file names, newest first */
  decisions: string[];
}

/** GET /api/project/<id>: everything the project page draws. */
export interface ProjectPayload {
  project: Project;
  /** the project and its members, by id: what its running section is drawn from */
  mine: Record<string, Project>;
  group: { id: string; name: string } | null;
  members: Project[];
  /** its open conversations, rounds folded (conversations.collapse) */
  rows: Surface[];
  statuses: Record<string, SessionStatus>;
  doing: Record<string, string>;
  spent: Record<string, number>;
  returned: Record<string, number>;
  tree: Record<string, TreeNode>;
  /** answer batches per page artifact */
  histories: Record<string, number>;
  closed: ClosedPage[];
  /** work launched here with no page yet (statusd.starting_tasks) */
  starting: Pageless[];
  /** where launched work runs, "" when nowhere */
  where: string;
  /** what a task launched here is told */
  prompt: string;
  context: ProjectContext | null;
  /** what the launcher's skill picker offers (unfiltered library.scan) */
  library: InstalledEntry[];
  fleet_page_code: { stale?: boolean; since?: number; head?: string };
}

/** Who answers a page's feedback (surface.ownership). */
export interface PageOwner {
  state: string;
  task?: string;
  kind?: string;
  why?: string;
  error?: string;
}

/** GET /api/page/<key>: one conversation and the repairs that apply to it. */
export interface PagePayload {
  key: string;
  /** the conversation: its newest round, with the rest as `earlier` */
  row: Surface;
  /** every project a page can be moved to */
  projects: string[];
  /** conversations that can replace it, same project first */
  candidates: { key: string; title: string }[];
  owner: PageOwner | null;
}
