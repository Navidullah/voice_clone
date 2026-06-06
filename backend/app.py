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
import json
import uuid
import time
import hmac
import math
import threading
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
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
META_PATH = VOICES_DIR / "voices.json"  # voice_id -> {name, created_at, duration_sec}
VOICES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

# Guards concurrent read-modify-write of the voices.json metadata sidecar.
_meta_lock = threading.Lock()


def _read_meta() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _set_meta_entry(voice_id: str, entry: dict) -> None:
    with _meta_lock:
        meta = _read_meta()
        meta[voice_id] = entry
        META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _delete_meta_entry(voice_id: str) -> None:
    with _meta_lock:
        meta = _read_meta()
        if meta.pop(voice_id, None) is not None:
            META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    return name[:60] if name else "My voice"

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
    """List every voice stored on this server, newest first, with metadata.

    Note: this returns ALL voices on the machine, so it's meant for local/admin
    use. The public website builds its per-visitor library from the browser's
    own localStorage instead (a shared server list would leak voices between
    visitors).
    """
    with _meta_lock:
        meta = _read_meta()
    voices = []
    for p in VOICES_DIR.glob("*.wav"):
        m = meta.get(p.stem, {})
        voices.append({
            "voice_id": p.stem,
            "name": m.get("name", "Untitled voice"),
            "created_at": m.get("created_at"),
            "duration_sec": m.get("duration_sec"),
        })
    voices.sort(key=lambda v: v["created_at"] or 0, reverse=True)
    return {"voices": voices}


@app.delete("/api/voices/{voice_id}", dependencies=[Depends(require_api_key)])
def delete_voice(voice_id: str):
    """Delete a stored sample and its metadata."""
    path = _voice_path(voice_id)  # validates voice_id (path-traversal guard)
    existed = path.exists()
    if existed:
        path.unlink()
    _delete_meta_entry(voice_id)
    if not existed:
        raise HTTPException(404, "unknown voice_id")
    return {"deleted": True}


@app.get("/api/voices/{voice_id}/sample", dependencies=[Depends(require_api_key)])
def voice_sample(voice_id: str):
    """Stream a stored reference sample back (used for library preview)."""
    path = _voice_path(voice_id)
    if not path.exists():
        raise HTTPException(404, "unknown voice_id")
    return FileResponse(str(path), media_type="audio/wav")


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
    name: str = Form(""),
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

    display_name = _clean_name(name)
    _set_meta_entry(voice_id, {
        "name": display_name,
        "created_at": time.time(),
        "duration_sec": round(duration, 2),
    })
    return {
        "voice_id": voice_id,
        "name": display_name,
        "duration_sec": round(duration, 2),
        "warnings": warnings,
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _pitch_shift(audio: "np.ndarray", sr: int, semitones: float) -> "np.ndarray":
    """Formant-preserving pitch shift via the WORLD vocoder.

    WORLD keeps the spectral envelope (formants) fixed and only moves F0, so a
    shifted voice sounds like the same speaker at a lower/higher pitch instead
    of the metallic, transient-smeared result of a phase vocoder — much better
    for speech. Falls back to librosa if WORLD fails for any reason.
    """
    try:
        import pyworld as pw
        x = np.ascontiguousarray(audio, dtype=np.float64)
        f0, t = pw.harvest(x, sr)          # robust F0 contour
        sp = pw.cheaptrick(x, f0, t, sr)   # spectral envelope (formants) — kept fixed
        ap = pw.d4c(x, f0, t, sr)          # aperiodicity
        f0_shifted = f0 * (2.0 ** (semitones / 12.0))
        y = pw.synthesize(f0_shifted, sp, ap, sr)
        return np.ascontiguousarray(y, dtype=np.float32)
    except Exception:
        import librosa
        return librosa.effects.pitch_shift(
            np.ascontiguousarray(audio, dtype=np.float32), sr=sr, n_steps=float(semitones)
        )


def _postprocess(wav: "torch.Tensor", sr: int, pitch: float, speed: float) -> "np.ndarray":
    """Downmix to mono float32 and apply pitch-shift / time-stretch (CPU).

    pitch is in semitones (+ = higher); speed is a tempo multiplier that
    preserves pitch. Both are skipped when at their no-op defaults, so the
    default request path pays no extra cost.
    """
    audio = wav.detach().cpu().float().numpy()
    if audio.ndim > 1:
        audio = audio.mean(axis=0)  # downmix to mono
    audio = np.ascontiguousarray(audio, dtype=np.float32)

    if abs(pitch) > 1e-6:
        audio = _pitch_shift(audio, sr, pitch)
    if abs(speed - 1.0) > 1e-6:
        import librosa  # phase-vocoder time-stretch is fine for tempo
        audio = librosa.effects.time_stretch(
            np.ascontiguousarray(audio, dtype=np.float32), rate=float(speed)
        )
    return np.ascontiguousarray(audio, dtype=np.float32)


def _encode(audio: "np.ndarray", sr: int, fmt: str) -> tuple[io.BytesIO, str, str]:
    """Encode a mono float32 signal to wav or mp3. Returns (buffer, media_type, ext)."""
    # Guard against clipping noise: scale down (never up) if a post-processing
    # step pushed the peak above full scale.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.999:
        audio = audio * (0.99 / peak)
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    if fmt == "mp3":
        from pydub import AudioSegment  # needs ffmpeg on PATH
        seg = AudioSegment(pcm16.tobytes(), frame_rate=int(sr), sample_width=2, channels=1)
        buf = io.BytesIO()
        seg.export(buf, format="mp3", bitrate="192k")
        buf.seek(0)
        return buf, "audio/mpeg", "mp3"

    # WAV: write 16-bit PCM via torchaudio (consistent with the rest of the app).
    buf = io.BytesIO()
    torchaudio.save(buf, torch.from_numpy(pcm16).unsqueeze(0), int(sr), format="wav")
    buf.seek(0)
    return buf, "audio/wav", "wav"


@app.post("/api/generate", dependencies=[Depends(require_api_key)])
def generate(
    request: Request,
    voice_id: str = Form(...),
    text: str = Form(...),
    exaggeration: float = Form(0.5),
    cfg_weight: float = Form(0.5),
    temperature: float = Form(0.8),
    speed: float = Form(1.0),
    pitch: float = Form(0.0),
    format: str = Form("wav"),
):
    """Generate speech in the cloned voice.

    Controls (all optional, clamped server-side):
      exaggeration  expressiveness / emotional intensity (Chatterbox)
      cfg_weight    pacing & adherence to the reference (Chatterbox)
      temperature   sampling variation (Chatterbox)
      speed         playback tempo, pitch-preserving (post-process)
      pitch         tone shift in semitones, -6..+6 (post-process)
      format        "wav" (default) or "mp3"
    """
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

    exaggeration = _clamp(exaggeration, 0.25, 2.0)
    cfg_weight = _clamp(cfg_weight, 0.0, 1.0)
    temperature = _clamp(temperature, 0.1, 1.5)
    speed = _clamp(speed, 0.5, 2.0)
    pitch = _clamp(pitch, -6.0, 6.0)
    fmt = "mp3" if str(format).lower() == "mp3" else "wav"

    # Serialize GPU access so concurrent requests don't exhaust VRAM.
    with GPU_LOCK:
        wav = MODEL.generate(
            text,
            audio_prompt_path=str(ref),
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
            temperature=temperature,
        )

    # Fast path: default WAV with no pitch/speed change → original behavior.
    if fmt == "wav" and abs(pitch) < 1e-6 and abs(speed - 1.0) < 1e-6:
        buf = io.BytesIO()
        torchaudio.save(buf, wav, MODEL.sr, format="wav")
        buf.seek(0)
        return StreamingResponse(buf, media_type="audio/wav")

    # CPU post-processing happens OUTSIDE the GPU lock.
    audio = _postprocess(wav, MODEL.sr, pitch, speed)
    buf, media_type, ext = _encode(audio, MODEL.sr, fmt)
    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="voice-clone.{ext}"'},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
