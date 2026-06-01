import { NextRequest, NextResponse } from "next/server";
import { backendUrl, serverKeyHeader, forwardedForHeader, passThrough } from "../_lib";

// Multipart upload → must run on the Node runtime, not the Edge runtime.
export const runtime = "nodejs";

/**
 * Proxies a voice-sample upload to the voice-clone backend.
 * The API key lives only on the server (never sent to the browser).
 */
export async function POST(req: NextRequest) {
  const incoming = await req.formData();

  const sample = incoming.get("sample");
  if (!(sample instanceof File)) {
    return NextResponse.json({ error: "missing 'sample' file" }, { status: 400 });
  }

  // Rebuild the form for the backend (forward the consent flag verbatim).
  const out = new FormData();
  out.append("sample", sample, sample.name || "sample.wav");
  out.append("consent", String(incoming.get("consent") ?? "false"));

  const res = await fetch(backendUrl("/api/clone"), {
    method: "POST",
    headers: {
      ...serverKeyHeader(),
      ...forwardedForHeader(req), // so the backend rate-limits per visitor, not per server
    },
    body: out,
  });

  return passThrough(res); // forwards status + JSON (e.g. 401/403/429) unchanged
}
