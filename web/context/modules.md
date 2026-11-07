---
updated: 2026-11-05
lld-run: record
container: web (the React fleet UI, served by statusd.py at /app/)
---
# Modules: web

Dependency direction is fixed: `ui → api → model`, `ui → model`; `model`
depends on nothing. Each module is reached only through its `index.ts`
(checked by `scripts/check_boundaries.mjs`). Design gate: `python3 scripts/design-check`.

## model
- **Owns:** statusd.py's home-page logic in TypeScript: folding rounds (`collapse`), items, states, the Idle limit, the item graph, find, the view (`layout`), the New work lookups. Pure: no React, no network, no DOM.
- **Entry:** `src/model/index.ts` (only public import path)
- **Public API:** snapshot at `api/model.api.d.ts`
- **May depend on:** nothing · **Must not depend on:** `api`, `ui`, any package
- **Hides:** the Python rules each function copies (named after the Python function, so a change on one side is findable on the other); the payload's version skew
- **Decision:** context/decisions/0001-web-module-design.md

## api
- **Owns:** every call to the Python fleet server: `/api/fleet`, `/api/newwork`, `/fs`, the `/fleet/events` stream, and the Python page's form posts.
- **Entry:** `src/api/index.ts` (only public import path)
- **Public API:** snapshot at `api/api.api.d.ts`
- **May depend on:** `model` (types only) · **Must not depend on:** `ui`, React
- **Hides:** URLs; how a form post lands (asked for JSON, statusd answers `{notice, location}` instead of its 303); stream events and the fallback poll
- **Contract:** `contracts/statusd.openapi.json` (every endpoint the app may call; checked by `tests/test_web_contract.py`) · **Decision:** context/decisions/0002-statusd-http-contract.md

## ui
- **Owns:** drawing the home page and the library, costs, runs, session-tree, project and manage screens exactly as statusd.py draws them (render_home, render_library, render_costs, render_runs, render_tree, render_project, render_page): same elements, same classes, the Python page's stylesheet (`src/ui/fleet.css`, generated). `ui/router.ts` picks the screen by path; `ui/server.tsx` the machine.
- **Entry:** `src/ui/index.ts` (only public import path: `App`)
- **Public API:** snapshot at `api/ui.api.d.ts`
- **May depend on:** `model`, `api`, react · **Must not depend on:** anything under another module's directory
- **Hides:** the page script's DOM habits it reproduces: items re-placed in last layout's order, find reading visible text, a "reload" after a plain form post

## Shared building blocks — use these, do not re-create
| Need | Use | Not |
|---|---|---|
| "3m ago", "$1,176", task-id labels, `/view/` links | `relTime`, `usd`, `taskLabel`, `viewHref`, `treeHref` in `src/model/format.ts` | local date or money formatting |
| Read a payload field that may be missing or skewed | `num`, `s` in `src/model/format.ts` | `Number(x)` / `String(x)` (they turn null into `0` / `"null"`) |
| Look a project, task or page up by name, as a Python dict would | `hasKey`, `own` in `src/model/format.ts`, or a `Map` | `key in obj` / `obj[key]` ("constructor" is on every object) |
| Format a number as Python's `:.2f` / `:,.0f` | `pyFixed` (and `usd`) in `src/model/format.ts` | `toFixed` (rounds an exact half up, Python rounds it to even) |
| A page's live state | `statusOf` in `src/model/conversations.ts` | `statuses[keyOf(sf)] \|\| {}` by hand |
| Turn a form into the body the browser would post | `formBody` in `src/api/index.ts` | `new URLSearchParams(new FormData(f) as …)` casts |
| A session key from a surface | `keyOf` in `src/model/conversations.ts` | splitting `path` by hand |
| Python's sort order for names | `cmp` in `src/model/format.ts` | `localeCompare` (orders differently) |
