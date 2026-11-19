---
level: module
status: accepted
date: 2026-11-19
containers: [web]
firmness: settled by the driver on green, 18-19 Nov 2026
---
# 0004 · Moving between the cadre app's screens never reloads the page

## Context
Every tab, page icon and cadre in the rail was a link that loaded a new page.
Between the old page going away and the app drawing the new one, the browser
showed a blank (white) screen: in Chrome a cross-page view transition hid it,
in other browsers it flashed on every click. The driver asked for smooth,
animated moves between tabs and cadres instead.

## Decision
The cadre app's screens (Holo at `/`, a cadre at `/c/<id>`, its tabs at
`/c/<id>/<tab>`) are one page. A plain click on a link to one of them, while
one of them is showing, moves in the app: `router.goInApp` pushes the address
and dispatches `popstate`; `App` redraws the named screen on `popstate` (the
same path back and forward take), inside `document.startViewTransition` where
the browser has it, so the rail, the page icons and the tabs hold still and
the old screen crossfades into the new one. A move of ours starts the new
screen at its top; back and forward keep the browser's scroll restoration. The
shell's navigation is fetched per cadre and drawn only for the cadre it was
fetched for; each cadre's screen is keyed by cadre, so it starts fresh.

Every other screen (a page in its frame, a run's tree, costs, runs, a project)
still loads as a page of its own (`router.go`), and modified or middle clicks
keep the browser's behaviour.

## Rejected
- **Rely on cross-document view transitions alone** — Chrome only; every other
  browser kept flashing.
- **Route every screen in the app** — the other screens read their state from
  the address once, at load; making all of them in-app is a separate change.
