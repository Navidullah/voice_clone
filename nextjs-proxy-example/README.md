# Next.js server-side proxy for the voice-clone backend (shopyor.com)

Lets your website expose the voice-clone feature **without ever shipping the API key
to the browser**. Visitors call your same-origin `/api/voice/*` routes; those routes run
on your server, attach the secret key, and forward to the Python backend.

```
Browser ──/api/voice/clone──▶ Next.js route (adds X-API-Key) ──▶ http://localhost:8000/api/clone
        ◀──── json/audio ─────                                 ◀────
```

## Files (copy into your Next.js App Router project)

```
app/api/voice/_lib.ts                 shared helpers (env, key header, IP forwarding)
app/api/voice/clone/route.ts          POST proxy → /api/clone
app/api/voice/generate/route.ts       POST proxy → /api/generate (streams WAV back)
app/voice-clone/page.tsx              demo UI (sends NO key — same-origin only)
.env.local.example                    env template
```

Just merge the `app/` folders into your existing `app/` directory.

## Setup

1. Copy env vars:
   ```
   cp .env.local.example .env.local
   ```
   Set `VOICE_CLONE_URL` (default `http://localhost:8000`) and `VOICE_CLONE_API_KEY`
   (the key from the backend's `backend/.env`).

2. Make sure the Python backend is running with `TRUST_PROXY = True` (so the visitor's
   real IP — forwarded by these routes via `X-Forwarded-For` — is what gets rate-limited,
   not your website server's IP).

3. Run your site (`npm run dev`) and open `/voice-clone`.

## Why this is the safe pattern

- The key is read from a **server-only** env var (no `NEXT_PUBLIC_` prefix), so it is never
  included in the browser bundle or visible in DevTools.
- The browser only ever talks to your own origin (`shopyor.com`), so you can keep CORS on
  the backend locked down to just your server.

## Going to production later

When you move the backend off `localhost` to a GPU server:
- Set `VOICE_CLONE_URL` to that server's URL (ideally over HTTPS / a private network).
- Lock the backend's CORS `allow_origins` to your domain.
- Keep the backend itself not publicly reachable if possible — only your Next.js server
  needs to reach it.
