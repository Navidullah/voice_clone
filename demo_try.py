"""One-off demo: generate a real speech reference, clone it, and save output."""
import time, torch, torchaudio
from chatterbox.tts import ChatterboxTTS

dev = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading model on {dev}...")
t0 = time.time()
m = ChatterboxTTS.from_pretrained(device=dev)
print(f"  loaded in {time.time()-t0:.1f}s")

# 1) Make a real *speech* reference (Chatterbox's built-in voice).
print("Generating reference sample...")
ref = m.generate("This is a reference recording of my natural speaking voice for the cloning test.")
torchaudio.save("demo_reference.wav", ref, m.sr)
print(f"  demo_reference.wav  ({ref.shape[-1]/m.sr:.1f}s)")

# 2) Clone that voice saying something new.
print("Generating cloned output...")
out = m.generate(
    "Hi! This sentence was spoken by a cloned voice. The tool is working end to end.",
    audio_prompt_path="demo_reference.wav",
)
torchaudio.save("demo_output.wav", out, m.sr)
print(f"  demo_output.wav  ({out.shape[-1]/m.sr:.1f}s)")
print("Done.")
