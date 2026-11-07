---
level: module
status: accepted
date: 2026-11-05
container: web
---
# 0001 · The React fleet UI as three modules: model, api, ui

## Context
The React app in web/ must draw the fleet home page exactly as statusd.py
does (the parity ask). In round 1 it was one 300-line `App.tsx` doing data
fetching, the list logic and drawing, with the ported logic in `lib/` and
the server calls in a loose `api.ts`.

## Decision
- **model** (`src/model/`) holds statusd.py's home-page logic, pure. One
  entry point, `buildHome(fleet, view, now, origin)`, turns a payload into
  what is drawn; `layout()` is the page script's re-layout. Each function is
  named after the Python function it copies.
- **api** (`src/api/`) is the only code that knows a URL.
- **ui** (`src/ui/`) draws; it keeps only browser state (what is open, the
  find box, the kept view) and reproduces the Python page's DOM habits in
  two hooks (`useWorkLayout`, `useFind`).
- Imports go `ui → api → model`, `ui → model`, through each `index.ts`;
  `scripts/check_boundaries.mjs` fails otherwise. Public APIs are locked by
  snapshot (`api/*.api.d.ts`), every public symbol documented.

## Rejected alternative (designed twice)
**Python sends the list already shaped** (a view model from statusd; React
a pure view). One copy of the rules instead of two, but it refactors the
live `_items_html` path, and once `/` is served from React the Python
renderer is deleted anyway, which ends the duplication. Revisit if the
Python page is kept alongside React for long, or if a second client (a
desktop or phone shell) needs the same shaping without a browser.

## Reuse decisions (record run, 2026-11-05)
| Planned public symbol | Decision | Evidence searched |
|---|---|---|
| `buildHome` (model) | new: the body of round 1's `App` useMemo, moved; ports `render_fleet(by_item=True)` + `_items_html` ordering | `grep -rn "buildHome\|homeModel\|_items_html" web/src statusd.py`: only the Python original |
| `limitChoices` (model) | new: ports `statusd.work_toolbar`'s `sorted({6, 15, 30, 50, limit})` plus the page script's appended option | same grep for `limits = sorted`: statusd.py:2385 only |
| `staleCode` (model) | new: ports `statusd.code_line` as text; was a component in round 1 | grep `code_line\|staleCode`: one prior copy, in round 1's App.tsx (moved, not duplicated) |
| `folderPicks` (model) | new: ports `_new_work_form`'s `dict.fromkeys(...)` folder buttons; was inline in NewWork.tsx | grep `dict.fromkeys\|folderPicks`: statusd.py:2966 only |
| `formBody` (api) | extract: round 1 built the same body in two places with a cast | grep `new FormData`: App.tsx and api.ts in round 1 (two sites; extracted because the cast was a type hole, not for reuse) |
| `Tree`, `ServerView`, `LayoutInput` (model types) | new exports of existing shapes, so public signatures name only public types | snapshot showed `ItemContext` referring to unexported `Tree` |

## Known duplication (baseline 0.54%)
The combobox keyboard handling in `ui/DirField.tsx` and `ui/NewWork.tsx`
(RunPicker) is one 17-line clone. Not extracted: two call sites (rule of
three), and the two differ on Enter with nothing marked (the skill picker
takes the first match, the folder field does nothing), exactly as the
Python page's two scripts differ. Extract when a third combobox appears.

## Public API changes after review (2026-11-05, round 2)
Not purely additive, so listed here rather than auto-approved:
- `Fleet.snap.code` typed as `{running, tree, since, head}` instead of any
  record (it was read with casts).
- `Fleet.archived` removed: statusd now leaves archived rows out of
  `/api/fleet` itself, so no reader re-applies the filter.
- `postForm` returns a `Landing` (`notice`, `path`, `location`) instead of
  the notice alone, so a post that lands off the home page (a refused
  Archive) navigates there as the Python page does.

## Public API change, round 5 (2026-11-06): one machine's API is a value
`fetchFleet`, `fetchNewWork` and `fetchDirs` became methods of `Server`,
returned by `connect(base)`; `fetchBackends` lists the machines. The driver
chose that the app switches between machines and talks to each directly
(web/context/decisions/0002, amendment), so every call has to know which
machine it is for. The ui gets the chosen machine's `Server` from a React
context (`ui/server.tsx`), never by building URLs itself.
