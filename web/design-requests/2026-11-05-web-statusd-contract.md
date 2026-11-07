---
from: low-level-design
to: architecture
kind: split
modules: [api]
status: applied
slice: the React fleet UI parity work (branch react-fleet)
---
## Needed
A declared process boundary between the web app (browser) and statusd.py:
which HTTP endpoints the app may rely on, and whether that boundary gets a
contract file (OpenAPI) that both sides are checked against.

## Why the current interface cannot work
It works, but nothing declares it. The app depends on `/api/fleet`,
`/api/newwork`, `/fs`, `/fleet/events`, and the Python page's form posts
(`/tasks`, `/items`, `/repair`, `/projects`) with their 303-to-`?notice=`
redirects. A change to any of them in statusd.py is caught only if someone
runs the parity harness. The remaining screens (library, costs, runs, cards,
projects, repair) each need a new JSON endpoint, so the boundary is about to
grow by six.

## Tried within the current signature
Typing the payload in `src/model/payload.ts` (done; catches nothing on the
Python side). Python tests for the new endpoints (done for `/api/newwork`).

## Proposed
Architecture decides whether the boundary is declared (an ADR naming the
endpoints) and whether it is contract-checked (OpenAPI under web/contracts/,
oasdiff in the gate). Until then the api module is marked blocked-on:
architecture in the module map.

## Resolution (architecture, 2026-11-06)
Declared and contract-checked: context/decisions/0002-statusd-http-contract.md.
The contract is `contracts/statusd.openapi.json` (OpenAPI 3.1 as JSON). It is
checked by `tests/test_web_contract.py` in the Python suite, using
`scripts/contract_check.py`, a stdlib validator. oasdiff was not adopted
(see the ADR's rejected alternatives).
