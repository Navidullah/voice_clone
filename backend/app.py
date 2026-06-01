"""
Voice cloning backend (FastAPI + Chatterbox TTS).

Endpoints:
  GET  /                -> serves the test web page
  GET  /health          -> model/device status
  POST /api/clone       -> upload a reference voice sample (.wav/.mp3), returns a voice_id
  POST /api/generate    -> {voice_id, text}  -> returns generated speech as .wav
  GET  /api/voices      -> list saved voices

The model is loaded ONCE at startup and kept resident on the GPU.
"""

import io
import os
import uuid
import time
import hmac
import math
import threading
from collections import defaultdict, deque
from pathlib import Path

import torch
import torchaudio
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    from dotenv import load_dotenv  # loads keys from backend/.env if present
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from chatterbox.tts import ChatterboxTTS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = ROOT / "voices"      # stored reference samples, named <voice_id>.wav
OUTPUTS_DIR = ROOT / "outputs"    # generated clips (cache / debugging)
FRONTEND = ROOT / "frontend" / "index.html"
VOICES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

# Safety cap so a single request can't lock the GPU forever.
MAX_CHARS = 1000
# Reject oversized uploads (reference samples should be short).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

# Audio quality pre-check thresholds.
MIN_DURATION = 3.0     # seconds — below this is too little to clone (reject)
SHORT_DURATION = 8.0   # seconds — below this we warn (works, but quality suffers)
LONG_DURATION = 120.0  # seconds — above this we warn (unnecessarily long)
REJECT_DBFS = -45.0    # quieter than this RMS level = effectively silent (reject)
QUIET_DBFS = -32.0     # quieter than this = warn
REJECT_CLIP_FRAC = 0.05   # >5% of samples clipped (reject)
WARN_CLIP_FRAC = 0.005    # >0.5% clipped (warn)

# Set True only if you run behind a reverse proxy you trust (nginx/Caddy) that
# sets X-Forwarded-For. If False, the proxy's own IP is used for every client.
TRUST_PROXY = True

# Per-IP rate limits: (max_requests, window_seconds).
CLONE_LIMIT = (10, 3600)     # 10 uploads per hour
GENERATE_LIMIT = (30, 600)   # 30 generations per 10 minutes
GENERATE_BURST = (4, 60)     # ...and at most 4 per minute (protects the GPU)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Only one generation runs on the GPU at a time — prevents VRAM blow-ups when
# multiple website visitors hit /api/generate simultaneously.
GPU_LOCK = threading.Lock()


class RateLimiter:
    """Simple thread-safe sliding-window limiter, keyed by client IP."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return False, int(dq[0] + self.window - now) + 1
            dq.append(now)
            return True, 0


_clone_rl = RateLimiter(*CLONE_LIMIT)
_gen_rl = RateLimiter(*GENERATE_LIMIT)
_gen_burst_rl = RateLimiter(*GENERATE_BURST)


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(limiter: RateLimiter, key: str) -> None:
    ok, retry = limiter.check(key)
    if not ok:
        raise HTTPException(
            429,
            detail=f"Rate limit exceeded. Try again in {retry}s.",
            headers={"Retry-After": str(retry)},
        )


# --- API-key auth ---------------------------------------------------------
# Keys come from the VOICE_CLONE_API_KEYS env var (comma-separated), optionally
# via a backend/.env file. If NO keys are configured, auth is DISABLED so local
# dev works out of the box. Set at least one key to require authentication.
API_KEYS = {
    k.strip()
    for k in os.getenv("VOICE_CLONE_API_KEYS", "").split(",")
    if k.strip()
}


def _key_is_valid(candidate: str) -> bool:
    # Constant-time comparison against every configured key.
    return any(hmac.compare_digest(candidate, k) for k in API_KEYS)


def require_api_key(request: Request) -> None:
    """FastAPI dependency. No-op when no keys are configured."""
    if not API_KEYS:
        return  # auth disabled

    candidate = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
    if not candidate:
        candidate = request.headers.get("x-api-key", "").strip()

    if not candidate or not _key_is_valid(candidate):
        raise HTTPException(
            401,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

app = FastAPI(title="Voice Clone")

# Which website origins may call this API *directly from a browser*.
# Read from ALLOWED_ORIGINS (comma-separated) so you don't edit code per-env.
# Defaults cover local dev + shopyor.com. Use "*" to allow any origin (dev only).
#
# Note: when you call this API through a SERVER-SIDE proxy (the recommended
# pattern, e.g. nextjs-proxy-example/), CORS does not apply — the request comes
# from your website's server, not the browser. CORS only gates direct browser
# calls, so this mainly hardens against other sites embedding your API.
_default_origins = (
    "http://localhost:8000,http://127.0.0.1:8000,"
    "https://shopyor.com,https://www.shopyor.com"
)
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Loaded in startup_event().
MODEL: ChatterboxTTS | None = None


@app.on_event("startup")
def startup_event() -> None:
    global MODEL
    print(f"[startup] Loading Chatterbox on {DEVICE} ...")
    t0 = time.time()
    MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
    print(f"[startup] Model ready in {time.time() - t0:.1f}s")


def _voice_path(voice_id: str) -> Path:
    # Reject anything that isn't a clean uuid-ish token to avoid path traversal.
    if not voice_id.replace("-", "").isalnum():
        raise HTTPException(400, "invalid voice_id")
    return VOICES_DIR / f"{voice_id}.wav"


@app.get("/health")
def health():
    return {
        "status": "ok" if MODEL is not None else "loading",
        "device": DEVICE,
        "cuda": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "auth_required": bool(API_KEYS),
    }


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/api/voices", dependencies=[Depends(require_api_key)])
def list_voices():
    return {"voices": [p.stem for p in VOICES_DIR.glob("*.wav")]}


def analyze_audio(wav: "torch.Tensor", sr: int) -> tuple[float, list[str]]:
    """Inspect a reference sample. Returns (duration_sec, warnings).

    Raises HTTPException(422) for samples that are unusable for cloning.
    """
    mono = wav.float().mean(dim=0)  # downmix to mono
    duration = mono.shape[-1] / sr

    rms = mono.pow(2).mean().clamp_min(1e-12).sqrt().item()
    dbfs = 20.0 * math.log10(rms) if rms > 0 else -120.0
    clip_frac = (mono.abs() >= 0.99).float().mean().item()

    errors, warnings = [], []

    if duration < MIN_DURATION:
        errors.append(f"Too short ({duration:.1f}s). Record at least {MIN_DURATION:.0f}s (10–30s is ideal).")
    elif duration < SHORT_DURATION:
        warnings.append(f"Sample is short ({duration:.1f}s) — 10–30s clones better.")
    elif duration > LONG_DURATION:
        warnings.append(f"Sample is long ({duration:.0f}s); a 10–30s clip is enough.")

    if dbfs < REJECT_DBFS:
        errors.append("Audio is nearly silent. Record louder / closer to the mic.")
    elif dbfs < QUIET_DBFS:
        warnings.append(f"Audio is quiet ({dbfs:.0f} dBFS). Closer to the mic would help.")

    if clip_frac > REJECT_CLIP_FRAC:
        errors.append(f"Heavy clipping ({clip_frac*100:.1f}% of samples). Lower the input level and re-record.")
    elif clip_frac > WARN_CLIP_FRAC:
        warnings.append(f"Some clipping detected ({clip_frac*100:.1f}%). Lower the input level for best results.")

    if errors:
        raise HTTPException(422, detail="; ".join(errors))

    return duration, warnings


@app.post("/api/clone", dependencies=[Depends(require_api_key)])
async def clone(
    request: Request,
    sample: UploadFile = File(...),
    consent: bool = Form(False),
):
    """Save an uploaded reference sample and return a voice_id to reuse later."""
    enforce(_clone_rl, client_ip(request))

    # Consent gate: the uploader must affirm they have the right to clone this voice.
    if not consent:
        raise HTTPException(
            403,
            "Consent required: you may only upload a voice you own or have explicit "
            "permission to clone.",
        )

    raw = await sample.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")

    # Decode whatever was uploaded (wav/mp3/flac/...) and re-save as a clean wav.
    try:
        wav, sr = torchaudio.load(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(400, f"could not read audio: {e}")

    # Quality pre-check: rejects unusable samples (422), warns on borderline ones.
    duration, warnings = analyze_audio(wav, sr)

    voice_id = uuid.uuid4().hex
    torchaudio.save(str(_voice_path(voice_id)), wav, sr)
    return {"voice_id": voice_id, "duration_sec": round(duration, 2), "warnings": warnings}


@app.post("/api/generate", dependencies=[Depends(require_api_key)])
def generate(request: Request, voice_id: str = Form(...), text: str = Form(...)):
    """Generate speech in the cloned voice."""
    if MODEL is None:
        raise HTTPException(503, "model still loading, try again shortly")

    ip = client_ip(request)
    enforce(_gen_burst_rl, ip)
    enforce(_gen_rl, ip)

    text = text.strip()
    if not text:
        raise HTTPException(400, "text is empty")
    if len(text) > MAX_CHARS:
        raise HTTPException(400, f"text too long (max {MAX_CHARS} chars)")

    ref = _voice_path(voice_id)
    if not ref.exists():
        raise HTTPException(404, "unknown voice_id (clone a voice first)")

    # Serialize GPU access so concurrent requests don't exhaust VRAM.
    with GPU_LOCK:
        wav = MODEL.generate(text, audio_prompt_path=str(ref))

    buf = io.BytesIO()
    torchaudio.save(buf, wav, MODEL.sr, format="wav")
    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
