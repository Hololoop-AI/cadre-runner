---
level: container-boundary
status: accepted
date: 2026-11-06
containers: [web, statusd]
firmness: firm for the peer tier, provisional for the app tier
answers: design-requests/2026-11-05-web-statusd-contract.md
---
# 0002 · The statusd HTTP boundary is declared and contract-checked

## Context
The React app (`web/`, served by statusd.py at `/app/`, same origin) depends
on `/api/fleet`, `/api/newwork`, `/fs`, `/fleet/events` and the Python page's
form posts. Nothing declared that interface: a change on the Python side was
caught only by the parity harness, which someone has to run by hand. Six
more screens are coming, each with a JSON endpoint.

`/api/fleet` and `/api/fleet/version` have a second reader: other
machines' statusd, at whatever version they run (`runnerlib/backends.py`).
That is real skew, not a hypothetical one: on 2026-11-06 the live main
checkout has no `/api/newwork` yet.

The driver delegated the standard calls ("your judgement will be great for
the vast majority of these"). The driver's own one-sentence version is
still to come; until then: *statusd says in one file what it answers, and
the tests fail when it, or the app, stops matching that file.*

## Decision
1. **One contract file:** `web/contracts/statusd.openapi.json`, OpenAPI 3.1
   written as JSON. JSON because both stacks parse it with no dependency
   (Python's `json`, Node's `JSON`). YAML would need a pip package or a
   hand-rolled parser.
2. **Endpoints in the contract:**

   | Endpoint | Tier | Reader |
   |---|---|---|
   | `GET /api/fleet/version` | peer | other fleets (polled), backends.py |
   | `GET /api/fleet` | peer | the app; other fleets |
   | `GET /api/newwork` | app | New work dialog |
   | `GET /fs?q=` | app | folder completion |
   | `GET /costs.json` | app | the costs screen (next) |
   | `GET /fleet/events` (SSE: `hello`, `source`, `change`) | app | the live dot and re-fetch |
   | `POST /tasks`, `/items`, `/repair`, `/projects` | app | forms |

   **Out:** the HTML pages (`/`, `/page/…`, `/project/…`, `/runs`,
   `/library`, `/cards`, `/tree/…`, `/find`, `/stranded/…`), the `/view/…`
   proxy to review-surface, `/app/…` static files, `/study`, `POST /cards`,
   `/projects/<id>/tasks|archive` (they come in with the project screen), and
   `/api/cards` (teammate deck sync between servers; a peer endpoint the app
   never reads; it joins the peer tier when it next changes).
3. **Compatibility, peer tier.** Additive only. A field is never removed,
   renamed or retyped. `version`, `instance` and `machine` stay required,
   and `graph` stays exactly three elements (older readers unpack it). Every
   other field may be missing, and readers treat a missing field as
   empty. Readers ignore fields they do not know. Capability is detected by
   presence (a missing field, or a 404 on a newer path), never by comparing
   versions; `version` is an opaque change token. A change that cannot be
   additive gets a new path (`/api/v2/fleet`), served beside the old one until
   no backend on any fleet page still reads the old one (in practice: until
   every machine has pulled). There is no version in today's paths.
4. **Compatibility, app tier.** The app and server ship from one checkout,
   so a change lands server, app and contract in the same PR. The built
   `dist/` can still be older than the running server (pulled and restarted
   without `npm run build`), so the app keeps reading fields defensively, as
   it already does (`num`, `s`, optional types).
5. **The schemas describe what statusd sends.** A response object statusd
   builds itself is closed (`additionalProperties: false`), so a new field
   fails the tests until it is declared. Records passed through from
   another component (status.json surfaces, review-surface page states,
   tree nodes, projects, cost-log turns) stay open. Closed applies to the
   producer check only; rule 3 still binds readers.
6. **What the app relies on is required.** A field the TypeScript reads as
   non-optional (`payload.ts`) must be `required` in the contract (for
   example `RunningCard.task`, `Project.id`/`name`, `LinkRecord.*`).
7. **Errors:** an HTTP status is the error. The body is human text
   (statusd's `send_error` page), not part of the contract; clients branch on
   the status only. A refusal the user should read is not an HTTP error: a
   form post answers it as a notice ("Refused: …", "Not launched: …").
   Posts use 403 for a cross-origin request (an `Origin` that is not this
   host), 413 for a body that is too large and 404 for an unknown action
   path.
8. **Form posts answer JSON when asked.** With `Accept: application/json`
   the answer is `200 {notice, location}` (location = `<path>?notice=…`, the
   page a browser would land on); without it, `303` to that location. Every
   JSON GET answers GET and HEAD with `Content-Type: application/json` and
   `Cache-Control: no-store`. ~~Same origin only: no CORS headers, ever.~~
   Amended 2026-11-06, see the end: a configured machine's page may read
   and post cross-origin.
9. **A new endpoint:** a new read is `GET /api/<screen>` (`/fs` and
   `/costs.json` keep their paths). In one PR: add the path to the contract
   with its `x-tier`, add the handler, add a case to
   `tests/test_web_contract.py`, and name the TypeScript interface that reads
   it with `x-ts`. The test fails if a contract operation has no case
   there, if the app calls a path the contract does not have, or if
   `payload.ts` has an interface no schema maps.

## How it is checked
`tests/test_web_contract.py` (stdlib, in `python3 -m pytest -q`) with
`web/scripts/contract_check.py`, a JSON Schema validator for the keywords
the contract uses; it refuses any other keyword instead of ignoring it.
The test:
- runs `statusd.Handler` on port 0 with every data source monkeypatched to
  realistic records, so `fleet_payload` and the handlers build their real
  answers, and validates every GET, every form post (JSON and plain, plus
  the 403), and the first three stream events;
- checks that the contract's operations are exactly the cases in the file;
- checks that `payload.ts` reads only declared fields, relies only on
  required ones, and maps every interface (`Earlier` is the one the app
  builds itself);
- checks that the app (`src/api/index.ts`, form actions and `postForm`
  targets in `src/ui/`) calls only declared paths;
- validates the parity fixtures (real frozen payloads, git-ignored) when
  they are present;
- proves the validator rejects six broken payloads.

Mutation run, 2026-11-06: retyping `slots`, adding an undeclared field,
renaming the `change` event, the app reading an undeclared field, and a
Landing without `location` each failed the suite.
`python3 web/scripts/contract_check.py http://<host>:8183` checks a live
server (including another machine) by hand; on 2026-11-06 the main
checkout's live payloads fit, with `/api/newwork` absent as expected.

**Not caught:** TypeScript field *types* (only names and optionality are
compared); the form field names the app posts; a peer running other code,
except by hand with the live check; additive-only discipline across commits
(no diff tool: a removed field shows only in review of the contract's diff);
the meaning of a value (a field that keeps its type but changes units).

## Rejected alternatives
- **Status quo: types in `payload.ts`, parity harness by hand.** It catches
  nothing on the Python side in CI, and the boundary is about to grow by six
  endpoints.
- **OpenAPI in YAML with `oasdiff` in the gate.** It would catch breaking
  diffs mechanically, but it needs a YAML parser (pip) and a Go binary that
  neither stack has. A closed producer schema plus review of a single JSON
  file covers today's risk. Revisit if a peer-tier break gets past review.
- **Generate the TypeScript types from the contract (`openapi-typescript`).**
  This removes the type-drift gap entirely, at the cost of one devDependency
  and rewriting `src/model/payload.ts`, whose locked model snapshot would
  change. Not worth it for one payload. **Revisit trigger:** when the
  screens add their third new endpoint type, or the first time a type
  mismatch reaches the app.
- **A version in every path (`/api/v1/…`) or a version header.** Peers
  already detect capability by presence, and a version would force every
  client to know a number it never needs. A new path is used only for a
  break.
- **A JSON error envelope (`{error}`).** No client branches on the kind of
  error today. Revisit when one has to (for example, a screen that tells
  "no such project" apart from "server down").
- **Validating peers at runtime** (statusd checking another machine's payload
  against the contract): that turns skew into an outage. Readers stay
  tolerant; validation is for this checkout's own answers.

## Revisit when
- a second client that is not same-origin (a Tauri or Capacitor shell)
  needs these endpoints: CORS or auth then becomes part of the boundary;
- the React app has to draw other machines. Today it reads only its own
  `/api/fleet`, while the Python home page draws every backend. Open with the
  driver as of 2026-11-06; the lean is an aggregate `GET /api/fleets`
  (app tier, built from `backends_mod.fleet`) over the app reading peers
  directly, which would need CORS;
- any trigger named in the rejected alternatives above.

## Amendment, 2026-11-06: the app talks to each machine directly
The driver chose this over a combined server endpoint: "this is the
intended design. We'd actually want something in the UI that lets us
switch between servers so they aren't all just on one page." So:

- **One machine at a time.** The app lists the machines from
  `GET /api/backends` on the server it was loaded from (app tier; this
  machine first with `url: ""`, then each `[[backends]]` entry), and a
  header switcher picks one. Every read, post and the stream then go to
  that machine directly; its links open on that machine.
- **CORS, allow-listed.** statusd answers its JSON reads, its JSON form
  answers and `/fleet/events` with `Access-Control-Allow-Origin` for, and
  accepts form posts from, exactly `statusd.CORS_ORIGINS`: the origins of
  its configured `[[backends]]` plus `[runner] cors_origins`. Any other
  origin is refused as before. No credentials: the API has no session.
  Every request the app makes is a CORS "simple" request (GET; POST of a
  urlencoded form with `Accept: application/json`), so there is no
  preflight to answer.
- **Version skew reaches the app tier.** The app may now read a machine
  running older code. App-tier reads it makes of another machine are
  best-effort: an endpoint that is missing there (an older `/api/newwork`)
  degrades its feature (no skill list) instead of failing the page.
- **Trust.** A machine trusts its configured peers' pages to post its
  forms. On a team backend that is every teammate's fleet listed under
  `cors_origins`; adding a teammate is a config line on the backend.

## Amendment, 2026-11-15: the surface frame is an app screen
`/view/<key>` was the last Python page around the app's own navigation: the
app sent the browser to it as a full page load, which an app shell that is
not served by a fleet (Capacitor) cannot follow. The driver asked for it to
become a React screen ("yea let's do it"), and for the design to hold when
the app is not served by a local server ("Ideally this would support
multiple backends / computers running the cadre backends"). So:

- **`GET /api/view/{key}` (app tier)** is what the frame reads: the session
  it frames, the page's title, and the dialogue's task (the study box).
- **`POST /study`** joins the contract: the frame's study box posts it.
- **The frame loads the chosen machine's `/session/<key>`.** The page
  server refuses framing; statusd's proxy rewrites its `frame-ancestors`
  to `'self'` plus `CORS_ORIGINS`, the same allow-list as the API. An app
  shell's own origin (`tauri://localhost`, `capacitor://localhost`) goes in
  `[runner] cors_origins` like any other.
- **The annotation layer stays the page server's.** The script that splits
  the agent's page into annotatable sections is served by review-surface
  inside the frame; the app does not reach into it. The driver: "Review
  surface serves it now though, by design."

## Amendment, 2026-11-18 to 20: Cadre owns the notification center
The driver asked for the notifications "to be accessible from any page of
the system", with a sound, and then: "Is there a way to use the centralized
notification center from anywhere in the system so that review surface never
needs to bother itself with notifications?" So:

- **`GET /api/notifications` (app tier) is statusd's own.**
  `runnerlib/notifications.py` reads the page server's event log
  (`events.jsonl`, the record it keeps for host apps) and decides what is
  news (one row per page, its latest event; thread pages left out). The
  page server has no bell or summary route of its own any more (removed in
  review-surface's own change, at the driver's choice, 18 Nov 2026).
- **A review page is always shown in the fleet's frame.** A top-level
  navigation to `/session/<key>` (`Sec-Fetch-Dest: document`) is answered
  with a redirect to `/view/<key>` (query kept, and the frame passes the
  query and `#fragment` on to the page); the frame's own load, a thread
  window (`?embed=`), clients without the header, and every request while
  `web/dist` is not built are forwarded unchanged. The
  frame runs the app and draws the bell in its bar, so the bell, the chime
  and read state follow the reader onto every review page with no
  notification code in the page server.
- **Pushed by the fleet stream, not polled:** the app re-reads the list when
  `/fleet/events` says `change`, which it says within about a second of any
  review-page event (statusd counts the page server's `/api/events` into its
  version). A tab's subscribers share one `EventSource` per URL
  (`api.subscribe`), so the bell costs no connection of its own against the
  browser's six per host.
- **Three tiers, and only the first chimes** (the driver's call, 19 Nov
  2026). *Needs you* (`actionable`: counted, chimes): a dialogue task's
  newest page whose task's newest turn ended (the runner's history log) at
  or after the page's last change, less 2 s (the page server logs the last
  write up to ~0.3 s after the stamp), unanswered since ("Your turn", or
  for a failed turn what its log says happened: "Run timed out", "Run
  interrupted" or "Run failed"; timed when the turn ended); or an agent's
  reply in a page's chat. A failed turn is also `problem`: a warning icon,
  the warning colour on the badge, and its own chime (the driver, 19 Nov
  2026: "a failure should get a special chime and possibly an icon"). *Working* (listed quietly): the task has a turn in
  flight in the runner's registry ("Working on it", timed from your answer
  or the agent's first write since, so rewrites mid-turn never move it).
  *Not listed*: side pages no dialogue owns, an earlier round's page, and a
  page you answered that the agent has neither started on nor written since.
  The browser remembers which rows it chimed for by page and time, so a turn
  that ended earlier than a row already rung still rings once. On blue, 24 h
  to 18 Nov 2026: 50 bell rows before, about 37 chimes after; the driver's
  one-task try-out on teal went from 7 chimes to 2.
- **The page you are on:** the bell's menu leaves out the page the frame
  shows (the page shows its own new version), but that page's news still
  chimes, so waiting on an agent in its own page rings when it answers.
- **Read state is the browser's,** in localStorage under the keys the page
  server's old bell used, so a reader's "seen" carries over.

Invariants (tested in `tests/test_notifications.py`, `src/api/subscribe.test.ts`,
`src/ui/bellStore.test.ts`):
- Cadre reads the page server's files only on its own machine, never writes
  them, honours its path variables (`REVIEW_SURFACE_STATE_DIR`,
  `REVIEW_SURFACE_LINKS`, `REVIEW_SURFACE_EVENTS`), and skips torn or
  foreign lines. It reads `events.jsonl`, the `thread-of` lines of
  `links.jsonl`, and `sessions[key].file` in `state.json` (re-read only when
  it changes; a half-written read keeps the last good copy). Review Surface's
  `AGENTS.md` names all three as read by hosts.
- Exactly one component answers `/api/notifications` for an origin: statusd.
- At most one `EventSource` per URL per tab.
- A new row chimes in one tab: under a Web Lock where the browser has them
  (HTTPS or localhost); over plain HTTP, tabs decide a random 0–250 ms apart
  and re-read what was chimed, which makes a double chime unlikely, not
  impossible.

Rollout: merge and deploy Cadre first, restart its statusd and reload open
Fleet tabs, then merge and deploy Review Surface. In between, a review page
shows two bells (its old one reads Cadre's list); the other order would leave
no bell at all.

Revisit when: a turn's placeholder can land within 2 s of the previous
turn's end (then stamp the page's last event into the turn record instead of
joining on time); answers start arriving other than as page feedback;
several Cadre instances share one page server; a bell fetch takes over 50 ms
or the history log passes 5 MB; the event log's last 256 KB no longer reach
back to the oldest page still waiting on you; the page server changes its
event log's format or place, or the `state.json` and `links.jsonl` fields
above; the page server runs on another machine than its Cadre; anything (a
shell, a webview, a browser on the page server's own port) shows review
pages without going through statusd; a second host app wants the same list.

## Amendment, 2026-11-22: the bell's button sits in the page's own bar
The frame's bar had grown to hold the bell, stacked over the page's own bar.
The driver asked for the bar as it was before the notification center: the
frame's bar slim again (the way back and the title), and the bell down in the
page's bar where the page server's own bell used to be. Cadre still owns the
list, the chime, the read state and the menu; only the button moved. So the
frame now talks to the page, by `postMessage` only (the page may be another
machine's), under a contract Review Surface owns (its README, "Framing
hosts"): the page offers (`review-surface:barHello`), the frame answers
`review-surface-host:carryBell` and sends `bellState` (count, failed run,
open, the button's name) on every change, and the page reports `bellClick`
(the button's box, so the menu hangs under it) and `bellClose` (Escape in
the page). `web/src/ui/PageBell.ts` is the one place that speaks it.

- **Either side can deploy first.** A page that loads without offering (an
  older page server) gets the button back in the frame's bar; an older frame
  never answers, so the page shows no bell.
- **The page cannot move the menu off the window**: the position it reports
  is clamped, and a resize closes a menu hung from it.
- **Revisit when** a second host frames review pages, or the page's bar wants
  more of the host's controls than the bell.
