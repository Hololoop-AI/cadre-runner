# Changelog

All notable changes to Cadre are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The version lives in `VERSION`;
`web/package.json` must match it (a test checks).

## [Unreleased]

### Changed
- Fleet: each cadre's pages are named tabs under the header on every screen,
  and quiet icons beside the cadre rail; folding the rail slides the icons away
  and the tabs stay. Moving between tabs, page icons and cadres happens in the
  app without a reload, with a crossfade where the browser supports it, so the
  screen never flashes. On a phone the tabs are one row that scrolls sideways.
- Pages say when they were written, beside the round, and link the sections
  and references they mention.
- Fleet: tooltips show after 300 ms; the board has find and opens a linked card
  (`?card=<id>`); ← back returns to the page you came from; a Done or Backlog
  card shows on its cadre's home while a run on it is working or came back
  unopened.

### Added
- Cadre's notification center: `GET /api/notifications` is answered by
  Cadre itself from the page server's event log; the page server no longer
  has a bell of its own. Every fleet screen has the bell at the right end of
  its header (one `TopBar`; a test refuses a screen that writes its own
  header), and so does the frame around a review page. A soft, low two-note chime
  plays once across tabs when something needs you: a dialogue's turn came
  back ("Your turn"; or "Run timed out", "Run interrupted", "Run failed") or an agent replied, the page you are
  on included. A failed run has its own falling chime, a warning icon and a
  warning-coloured badge. An agent at work shows as one quiet "Working on it" row;
  mid-turn rewrites and side pages are not listed. Mute it with the speaker
  button in the bell's menu. It updates
  from the fleet stream the tab already holds. A review page opened on its
  own now opens inside the fleet's frame.
- Learn, the study cadre: shipped beside Dev, Kitchen and Personal, with the
  five learning skills (shipped in `.agents/skills`), a Review tab (study
  problems of any shape, reviewed on a schedule), a home showing what is due,
  topics, sessions in progress and courses, its own icon, and three agents
  (tutor, course builder, card writer). Every study session is Learn's (the
  cadre record's `owns`) and also shows in the cadre that asked; costs and
  skills stay the owner's.
- The study hand-off over the Rust blackboard: a study request is an `ask` in
  namespace `learn`, topic `study`; the daemon's pass claims it and starts a
  session (or joins one open on the same topic); the session's end writes an
  `answer` into the asking cadre's namespace, posted onto the asking page and
  thread. `pipeline.py study`, the thread's offer and the Review tab's study
  box all go this way; with no board, a study still starts, without an answer.
  Every step survives a crash and repeats safely; a session left open 14 days
  is closed and answered.
- Skills held by Cadre: its own store (`pipeline.py skills add|list|remove`),
  its shipped skills, and any number of `skills_sources`. Each run gets them
  as its own Claude Code plugin (`--plugin-dir`), so nothing is written into
  the folder the work runs in any more.
- `runnerlib/rboard.py`, a stdlib client for the Rust board's socket,
  `pipeline.py study-ready`, and an opt-in live test against the machine's
  board (`BLACKBOARD_LIVE=1`).

### Changed
- The tutor, thread prompts, the deck and the Cards page speak of material,
  not code, so Learn works for any cadre.

## [0.1.0] - 2026-11-16

First versioned release. Everything before it is in git history.

### Added
- Dialogues: ask for something, an agent does it and writes a review page, you
  answer on the page, and the same session continues.
- The fleet page: every run, the kanban board, records, cards and study
  sessions, across one or more machines.
- A CI check that fails the build when a file names a real deployment.
- `pipeline.py --version`.

### Changed
- The fleet page listens on loopback (`127.0.0.1`) unless `status_bind` in the
  config opens it to another address. It used to listen on every network.
- Skills install to `~/.local/share/cadre/ai-dev-skills` by default; an existing
  clone in the old location is kept. `CADRE_SKILLS_REPO` points the installer at
  another skills repo.
