# Voice Clone

A local voice-cloning tool (zero-shot text-to-speech) built on **Chatterbox** (MIT licensed),
served via a FastAPI backend with a simple web UI. Runs on your NVIDIA GPU.

## ⚠️ Use responsibly
Only clone voices you own or have explicit permission to use. Cloning someone's voice
without consent may be illegal.

## Setup

```powershell
# 1. (one time) install dependencies
./venv/Scripts/python.exe -m pip install chatterbox-tts fastapi "uvicorn[standard]" python-multipart
# Then install the CUDA build of torch so it uses your GPU:
./venv/Scripts/python.exe -m pip install --force-reinstall torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
```

## Run

```powershell
./start.ps1
# or:
./venv/Scripts/python.exe backend/app.py
```

Then open http://localhost:8000

## How it works
1. **Upload** a 10–30s clean sample of the target voice → backend stores it, returns a `voice_id`.
2. **Generate** — type text; the model speaks it in that voice and returns a `.wav`.

The model is loaded once at startup and stays resident in GPU memory.

## Project layout
```
backend/app.py        FastAPI server + model
frontend/index.html   Test web UI
voices/               Uploaded reference samples (<voice_id>.wav)
outputs/              (reserved for generated clips)
```

## Embedding in your website
The API is CORS-enabled. From your site's frontend, call:
- `POST /api/clone`  (multipart: `sample` = audio file) → `{ voice_id }`
- `POST /api/generate` (multipart: `voice_id`, `text`) → audio/wav

Before going public: restrict `allow_origins` in `app.py` to your domain, add auth,
and run behind HTTPS (e.g. via a reverse proxy).

## Consent & rate limiting

Configurable at the top of `backend/app.py`:

- **Consent gate** — `/api/clone` requires `consent=true`; uploads without it are rejected (403).
  The web UI has a confirmation checkbox.
- **Rate limits** (per IP, in-memory sliding window):
  - `CLONE_LIMIT` — uploads per hour (default 10)
  - `GENERATE_LIMIT` — generations per 10 min (default 30)
  - `GENERATE_BURST` — generations per minute (default 4); over-limit requests get `429` + `Retry-After`
- **GPU lock** — generations are serialized so concurrent visitors can't exhaust 8GB VRAM.
- **`TRUST_PROXY`** — set `True` when behind nginx/Caddy so the real client IP (from
  `X-Forwarded-For`) is rate-limited, not the proxy's IP.
- **`MAX_UPLOAD_BYTES`** — caps reference-sample size (default 25 MB).

> Rate-limit state is in-memory: it resets on restart and isn't shared across multiple
> server processes. For a multi-worker deployment, back it with Redis.

## API-key authentication

Keys are read from `VOICE_CLONE_API_KEYS` (comma-separated), via the environment or a
`backend/.env` file (gitignored). **If no keys are set, auth is disabled** — handy for local
dev. Set at least one key to require authentication on `/api/clone`, `/api/generate`,
and `/api/voices`. `/health` and the page stay public.

Clients authenticate with either header:
```
Authorization: Bearer <key>
X-API-Key: <key>
```
Missing/invalid keys get `401`. Comparison is constant-time.

**Generate / rotate a key:**
```powershell
./venv/Scripts/python.exe -c "import secrets; print('vc_' + secrets.token_urlsafe(32))"
# put it in backend/.env as VOICE_CLONE_API_KEYS=..., then restart the server
```

### ⚠️ Don't expose the key in browser JavaScript
On a public website, a key shipped to the browser is visible to anyone. Two safe patterns:
1. **Server-side proxy (recommended):** your website's backend holds the key and forwards
   requests to this service. Visitors never see it.
2. **Per-user keys:** issue a separate key per logged-in user and rate-limit per key.

The included test page (`frontend/index.html`) stores a key in `localStorage` purely for
local testing — that is *not* a substitute for the patterns above in production.
