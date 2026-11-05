# Cadre fleet web app

The fleet home page as a React app (Vite + React + TypeScript). It runs in a
browser today; the same build is meant to go into a Tauri (desktop) or
Capacitor (phone) shell later.

The Python fleet server (`statusd.py`) serves the built `dist/` at **`/app/`**,
so Vite builds with `base: '/app/'`. Every API call is same-origin and
root-relative: `GET /api/fleet`, the `GET /fleet/events` stream (refetch on
`change`), `POST /tasks`, and review pages at `/session/<key>`.

```sh
npm install
npm run dev      # http://localhost:5183/app/, proxies the API to 127.0.0.1:8183
npm test         # vitest: "needs you" shaping and the New work form (mocked fetch)
npm run build    # tsc --noEmit, then vite build into dist/
```

In dev the browser's origin is `localhost:5183`, so the server's same-origin
check refuses `POST /tasks` from the dev server. Submit work from the build
served at `/app/`.
