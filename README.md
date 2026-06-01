# Voice Clone

A local **zero-shot text-to-speech** tool: upload a short sample of a voice, then make it
speak any text you type. Built on [Chatterbox](https://github.com/resemble-ai/chatterbox)
(MIT licensed), served by a FastAPI backend with a simple web UI. Runs on your NVIDIA GPU.

> ⚠️ **Use responsibly.** Only clone voices you own or have explicit permission to use.
> Cloning someone's voice without consent may be illegal in your jurisdiction.

---

## Requirements

- Windows with an NVIDIA GPU (developed on an RTX 3050, 8 GB VRAM)
- Python 3.12
- A CUDA-capable PyTorch build (see setup below)

## Setup

```powershell
# 1. Create the virtual environment (one time)
python -m venv venv

# 2. Install dependencies
./venv/Scripts/python.exe -m pip install chatterbox-tts fastapi "uvicorn[standard]" python-multipart

# 3. Install the CUDA build of PyTorch (the default pip install gives a CPU-only build)
./venv/Scripts/python.exe -m pip install --force-reinstall `
  torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
```

Verify the GPU is detected:

```powershell
./venv/Scripts/python.exe -c "import torch; print('cuda:', torch.cuda.is_available())"
# expected: cuda: True
```

## Run

```powershell
./start.ps1
# or:
./venv/Scripts/python.exe backend/app.py
```

The model loads into GPU memory once at startup (~15 s), then the server stays resident.
Open **http://localhost:8000** in your browser. Stop with `Ctrl+C`.

## How it works

1. **Clone** — upload a clean 10–30 s sample of the target voice (and confirm consent).
   The backend stores it and returns a `voice_id`.
2. **Generate** — type text; the model speaks it in that voice and returns a `.wav`.

---

## Project layout

```
backend/app.py          FastAPI server + model (loaded once at startup, port 8000)
backend/.env            API keys (gitignored — never committed)
frontend/index.html     Test web UI
nextjs-proxy-example/   Reference Next.js proxy for embedding in a public website
voices/                 Uploaded reference samples (<voice_id>.wav, gitignored)
outputs/                Generated clips (gitignored)
start.ps1               Convenience launcher
```

## API

| Method | Endpoint        | Body (multipart)            | Returns          |
|--------|-----------------|-----------------------------|------------------|
| POST   | `/api/clone`    | `sample` (audio), `consent` | `{ voice_id }`   |
| POST   | `/api/generate` | `voice_id`, `text`          | `audio/wav`      |
| GET    | `/api/voices`   | —                           | list of voices   |
| GET    | `/health`       | —                           | status + GPU info|

---

## Configuration

All settings live at the top of `backend/app.py` or in environment / `backend/.env`.

### Authentication

API keys are read from `VOICE_CLONE_API_KEYS` (comma-separated), via the environment or a
`backend/.env` file (gitignored). **If no keys are set, auth is disabled** — convenient for
local dev. With keys set, `/api/clone`, `/api/generate`, and `/api/voices` require one;
`/health` stays public. Clients send either header:

```
Authorization: Bearer <key>
X-API-Key: <key>
```

Missing/invalid keys get `401`. Comparison is constant-time. Generate a key with:

```powershell
./venv/Scripts/python.exe -c "import secrets; print('vc_' + secrets.token_urlsafe(32))"
# add it to backend/.env as VOICE_CLONE_API_KEYS=..., then restart the server
```

### Consent & rate limiting

- **Consent gate** — `/api/clone` requires `consent=true`; the web UI has a checkbox.
- **Rate limits** (per IP, in-memory sliding window):
  - `CLONE_LIMIT` — uploads per hour (default 10)
  - `GENERATE_LIMIT` — generations per 10 min (default 30)
  - `GENERATE_BURST` — generations per minute (default 4); over-limit → `429` + `Retry-After`
- **GPU lock** — generations are serialized so concurrent visitors can't exhaust VRAM.
- **`MAX_UPLOAD_BYTES`** — caps reference-sample size (default 25 MB).
- **`TRUST_PROXY`** — set `True` behind nginx/Caddy so the real client IP (from
  `X-Forwarded-For`) is rate-limited instead of the proxy's IP.

> Rate-limit state is in-memory: it resets on restart and isn't shared across processes.
> For a multi-worker deployment, back it with Redis.

---

## Embedding in a website

The API is CORS-enabled. **Never ship your API key to the browser** — anyone could read it.
Use a server-side proxy: your website's backend holds the key and forwards requests to this
service, so visitors never see it. A working reference is in
[`nextjs-proxy-example/`](nextjs-proxy-example/) (Next.js App Router routes that keep the key
server-side and forward the visitor's IP).

**Before going public:**
- Restrict `allow_origins` in `app.py` to your domain (currently `*`).
- Run behind HTTPS (e.g. a reverse proxy).
- Set at least one API key and keep it server-side.

## License

MIT — see [LICENSE](LICENSE). Chatterbox, the underlying TTS model, is also MIT licensed.
