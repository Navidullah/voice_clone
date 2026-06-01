"use client";

import { useState } from "react";

/**
 * Demo UI for the voice-clone feature.
 * It talks ONLY to your same-origin proxy routes (/api/voice/*) — the API key
 * is injected server-side and never reaches this browser code.
 */
export default function VoiceClonePage() {
  const [voiceId, setVoiceId] = useState<string | null>(null);
  const [text, setText] = useState("Hello! This is my cloned voice.");
  const [consent, setConsent] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleUpload(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const file = (form.elements.namedItem("sample") as HTMLInputElement).files?.[0];
    if (!file) return setStatus("Pick an audio file first.");
    if (!consent) return setStatus("Please confirm you have permission to clone this voice.");

    setBusy(true);
    setStatus("Uploading…");
    try {
      const fd = new FormData();
      fd.append("sample", file);
      fd.append("consent", "true");
      const res = await fetch("/api/voice/clone", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || "upload failed");
      setVoiceId(data.voice_id);
      setStatus(`Voice ready (${data.duration_sec}s). You can generate now.`);
    } catch (err) {
      setStatus((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleGenerate() {
    if (!voiceId) return setStatus("Upload a voice sample first.");
    if (!text.trim()) return setStatus("Enter some text.");
    setBusy(true);
    setStatus("Generating…");
    try {
      const fd = new FormData();
      fd.append("voice_id", voiceId);
      fd.append("text", text);
      const res = await fetch("/api/voice/generate", { method: "POST", body: fd });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "generation failed");
      }
      const blob = await res.blob();
      setAudioUrl(URL.createObjectURL(blob));
      setStatus("Done.");
    } catch (err) {
      setStatus((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main style={{ maxWidth: 640, margin: "40px auto", padding: "0 20px", fontFamily: "system-ui" }}>
      <h1>🎙️ Voice Clone</h1>
      <p style={{ color: "#666" }}>
        Upload a 10–30s clean voice sample, then type text to speak in that voice.{" "}
        <strong>Only clone voices you have permission to use.</strong>
      </p>

      <form onSubmit={handleUpload} style={{ border: "1px solid #ddd", borderRadius: 12, padding: 20, marginBottom: 16 }}>
        <h3>Step 1 — Voice sample</h3>
        <input type="file" name="sample" accept="audio/*" />
        <label style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span style={{ fontSize: 13 }}>
            I confirm this is my own voice, or I have explicit permission from the speaker to clone it.
          </span>
        </label>
        <button type="submit" disabled={busy} style={{ marginTop: 12 }}>Upload</button>
      </form>

      <div style={{ border: "1px solid #ddd", borderRadius: 12, padding: 20 }}>
        <h3>Step 2 — Generate</h3>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          style={{ width: "100%", minHeight: 90 }}
        />
        <button onClick={handleGenerate} disabled={busy || !voiceId} style={{ marginTop: 12 }}>
          Generate speech
        </button>
        {audioUrl && <audio src={audioUrl} controls style={{ width: "100%", marginTop: 14 }} />}
      </div>

      <p style={{ color: "#666", marginTop: 12, minHeight: 18 }}>{status}</p>
    </main>
  );
}
