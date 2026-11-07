# Cadre fleet web app

The fleet home page and the library, costs, runs, session-tree, project and
manage screens as a React app (Vite + React + TypeScript), drawn to be
identical to the pages `statusd.py` serves at `/`, `/library`, `/costs`,
`/runs`, `/tree`, `/project/<id>` and `/page/<key>`: the same elements, the same
classes, and the Python page's own stylesheet (`src/ui/fleet.css`, copied from
`statusd._CSS + _HOME_CSS` by `scripts/sync_fleet_css.py`). It runs in a
browser today; the same build is meant to go into a Tauri (desktop) or
Capacitor (phone) shell later.

The Python fleet server serves the built `dist/` at **`/app/`**, so Vite
builds with `base: '/app/'`. It is also what `/`, `/library`, `/costs`,
`/runs`, `/tree`, `/tree/<id>`, `/project/<id>` and `/page/<key>` answer with
(`statusd.REACT_ROUTE`); the Python pages for them stay at `/classic/…`. Set
`CADRE_FLEET_UI=python` (or `fleet_ui = "python"` under `[runner]`) to serve
the Python pages at `/` instead; without a build in `dist/` statusd serves them
anyway. Screens the app does not draw yet (`/view`, `/history`, `/stranded`,
`/cards`, `/study`) are still the Python pages.
The calls (all declared in `contracts/statusd.openapi.json`):
`GET /api/fleet` (everything the list is drawn from), `GET /api/newwork` (the
home directory and the skill picker's library), `GET /fs` (folder
completion), `GET /api/library`, `/api/costs`, `/api/runs`, `/api/tree`,
`/api/project/<id>`, `/api/page/<key>` (the other screens), `GET /api/backends` (the machine switcher), the
`GET /fleet/events` stream (re-fetch on `change`), and the Python page's own
form posts: `/tasks`, `/items`, `/repair`, `/projects`. With another machine
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

## Proving parity

`parity/` serves both pages from one frozen fleet and compares them, DOM and
screenshot, through every scenario in `parity/scenarios.cjs`:

```sh
python3 web/parity/capture.py http://127.0.0.1:8183 web/parity/fixture.json   # freeze a live fleet
python3 web/parity/vary.py web/parity/fixture.json web/parity/fixture-rare.json   # optional: rare states
python3 web/parity/serve.py web/parity/fixture.json 8197 &                      # Python at /, React at /app/
node web/parity/compare.cjs --fixture web/parity/fixture.json --out /tmp/parity
```

The parity server is statusd.py itself with the backend fetch answered from
the fixture and its clock stopped; item actions and Archive change an
in-memory copy, and nothing else a page posts is applied. Fixtures hold real
page titles, so they are git-ignored.
