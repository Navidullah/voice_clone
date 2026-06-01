import { NextRequest, NextResponse } from "next/server";

/**
 * Shared helpers for the voice-clone proxy routes.
 * Reads server-only env vars (NOT prefixed with NEXT_PUBLIC_, so they never
 * reach the browser bundle):
 *   VOICE_CLONE_URL      e.g. http://localhost:8000  (default)
 *   VOICE_CLONE_API_KEY  the secret key your backend expects
 */

const BASE = (process.env.VOICE_CLONE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const KEY = process.env.VOICE_CLONE_API_KEY ?? "";

export function backendUrl(path: string): string {
  return `${BASE}${path}`;
}

export function serverKeyHeader(): Record<string, string> {
  return KEY ? { "X-API-Key": KEY } : {};
}

/**
 * Forward the visitor's real IP so the backend's per-IP rate limiting applies
 * per visitor (not per website server). Requires TRUST_PROXY=True on the backend.
 */
export function forwardedForHeader(req: NextRequest): Record<string, string> {
  const xff = req.headers.get("x-forwarded-for");
  const ip = xff?.split(",")[0]?.trim() || (req as unknown as { ip?: string }).ip;
  return ip ? { "X-Forwarded-For": ip } : {};
}

/** Forward a backend JSON response (status + body + Retry-After) unchanged. */
export async function passThrough(res: Response): Promise<NextResponse> {
  const text = await res.text();
  const headers = new Headers({
    "content-type": res.headers.get("content-type") ?? "application/json",
  });
  const retry = res.headers.get("retry-after");
  if (retry) headers.set("retry-after", retry);
  return new NextResponse(text, { status: res.status, headers });
}
