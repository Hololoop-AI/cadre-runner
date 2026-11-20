# Cadre fleet web app

The fleet home page and the library, costs, runs, session-tree, project,
manage and page-frame screens as a React app (Vite + React + TypeScript), drawn to be
identical to the Python pages it replaced at `/`, `/library`, `/costs`,
`/runs`, `/tree`, `/project/<id>` and `/page/<key>`: the same elements, the same
classes, and their stylesheet (`src/ui/fleet.css`, copied from
`statusd._CSS + _HOME_CSS` by `scripts/sync_fleet_css.py`). It runs in a
browser today; the same build is meant to go into a Tauri (desktop) or
Capacitor (phone) shell later.

The Python fleet server serves the built `dist/` at **`/app/`**, so Vite
builds with `base: '/app/'`. It is also what `/`, `/library`, `/costs`,
`/runs`, `/tree`, `/tree/<id>`, `/project/<id>`, `/page/<key>` and `/view/<key>`
answer with (`statusd.REACT_ROUTE`); without a build in `dist/` those paths
are a 404 that says to build it. The Python pages they replaced are gone.
`/` is Hololoop, a card per cadre and none of their runs; `/c/<id>` (and
`/c/<id>/agents` and `/settings`; Dev also has `/kanban` and `/blackboard`) is one cadre's own app, holding only
the runs that cadre owns (`runnerlib/cadres.py`), with the rail, header tabs
and a foldable side column from `GET /api/nav?cadre=<id>` (one menu button on
a phone). `/view/<key>` is the frame around one agent page: it loads the chosen
machine's `/session/<key>` in an iframe, which that machine's proxy lets
this fleet and its configured peers frame (`statusd.framable_header`).
Screens the app does not draw yet (`/history`, `/stranded`, `/cards`) are
still the Python pages.
The calls (all declared in `contracts/statusd.openapi.json`):
`GET /api/fleet` (everything the list is drawn from), `GET /api/newwork` (the
home directory and the skill picker's library), `GET /fs` (folder
completion), `GET /api/library`, `/api/costs`, `/api/runs`, `/api/tree`,
`/api/project/<id>`, `/api/page/<key>`, `/api/view/<key>` (the other screens), `GET /api/backends` (the machine switcher), the
`GET /fleet/events` stream (re-fetch on `change`), `GET /api/notifications` (Cadre's notification center, `runnerlib/notifications.py`; re-read on each stream `change`), and the Python page's own
form posts: `/tasks`, `/items`, `/repair`, `/projects`, `/study`. With another machine
chosen, every call goes to that machine directly.

## How it is organized

Three modules under `src/`, each reached only through its `index.ts`, with a
fixed dependency direction (`ui → api → model`, `ui → model`):

| Module | What it owns |
|---|---|
| `src/model/` | statusd.py's home-page logic in TypeScript, pure (no React, no network). Each function is named after the Python one it copies (`collapse`, `fleetItems`, `itemFacts`, `assignStates`, `buildItemGraph` for `_graph`, `layout` for the page script's `layout()`); `buildHome` turns one `/api/fleet` payload into what is drawn. |
| `src/api/` | every call to the server: URLs, the fleet stream, the form posts and their `?notice=` redirects. |
| `src/ui/` | React components drawing the model exactly as the Python page draws it, and the hooks that reproduce its DOM habits. |

`context/modules.md` is the module map (what each owns and may depend on,
and the shared helpers to reuse); `context/decisions/` holds why. The
design gate checks it all:

```sh
python3 scripts/design-check   # types, import rules, map vs tree, API snapshots, duplication
python3 scripts/api_snapshot.py --all   # rewrite api/*.api.d.ts after a deliberate public-API change
```

```sh
npm install
npm run dev      # http://localhost:5183/app/, proxies the API to 127.0.0.1:8183
npm test         # vitest: the ported list logic
npm run build    # tsc --noEmit, then vite build into dist/
```

In dev the browser's origin is `localhost:5183`, so the server's same-origin
check refuses form posts from the dev server. Post from the build served at
`/app/`.

## Frozen fleets

`parity/capture.py` freezes a live fleet's `/api/fleet` into a fixture
(`vary.py` adds rare states); `tests/test_web_contract.py` checks every
`parity/fixture*.json` against the contract. They once fed a side-by-side
comparison with the Python pages; those pages are gone, and so is the
comparison. Fixtures hold real page titles, so they are git-ignored.
