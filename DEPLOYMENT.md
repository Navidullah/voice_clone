# Deployment: running the voice-clone tool live on shopyor.com

This guide wires the tool into your website so visitors can use it **while your PC is on**.
The Chatterbox model stays on your RTX 3050; a Cloudflare Tunnel exposes your local
backend at a permanent HTTPS URL, and your website calls it through a server-side proxy
that keeps your API key secret.

```
Visitor browser ─▶ shopyor.com (Next.js on Vercel)
                      │  /api/voice/* proxy route  (holds VOICE_CLONE_API_KEY)
                      ▼
              https://voice.shopyor.com   (Cloudflare Tunnel, stable HTTPS)
                      ▼
              cloudflared on your PC ─▶ http://localhost:8000 ─▶ RTX 3050
```

**Your setup at a glance**
- Domain `shopyor.com`: registered at **Hostinger**, DNS moving to **Cloudflare** (free).
- Website: hosted on **Vercel** (apex `216.198.79.1`, `www` → `cname.vercel-dns.com`).
- Backend: FastAPI on your PC, port 8000, GPU model resident.

> ⚠️ The live feature works **only while your PC is on** and both the backend and
> `cloudflared` are running. When the PC is off, the website's voice feature is down.

---

## Phase 1 — Move shopyor.com DNS to Cloudflare (one time, free)

Changing nameservers does **not** transfer your domain or change ownership — it stays
registered (and renewed) at Hostinger. It only changes *who answers DNS lookups*. Fully
reversible: paste Hostinger's nameservers back anytime.

1. **Create a free Cloudflare account** at https://dash.cloudflare.com/sign-up.
2. **Add a site** → enter `shopyor.com` → choose the **Free** plan.
3. Cloudflare **scans your existing DNS records**. When it finishes, **carefully verify**
   every record was imported — this is the step that protects your site and email:
   - `A` `shopyor.com` → `216.198.79.1`  (Vercel apex)
   - `CNAME` `www` → `cname.vercel-dns.com`  (Vercel www)
   - Any **MX** records (email) and **TXT** records (SPF/DKIM/domain verification).
     Missing an MX/TXT record can break email or site verification — re-add anything missing
     by comparing against Hostinger's current DNS zone.

   > 🔴 **Vercel gotcha:** set the **Vercel records (apex `A` and `www` CNAME) to
   > "DNS only" (grey cloud, NOT orange/proxied).** Vercel provides its own CDN + SSL;
   > proxying them through Cloudflare can cause "Too Many Redirects" / SSL errors.
   > Grey-cloud = traffic goes straight to Vercel, exactly as today.

4. Cloudflare shows you **two nameservers** like `xena.ns.cloudflare.com` /
   `rick.ns.cloudflare.com`. Copy them.
5. **In Hostinger** (hPanel → Domains → shopyor.com → DNS / Nameservers): switch from
   "Hostinger nameservers" to **"Use custom nameservers"** and paste Cloudflare's two.
   Save.
6. Back in Cloudflare, click **"Check nameservers"**. Activation usually takes minutes
   (can be up to 24h). Cloudflare emails you when active. Your site keeps working
   throughout (both old and new nameservers point to the same Vercel site).
7. Once active, in Cloudflare set **SSL/TLS → Overview → Full (strict)**. (Safe because
   the Vercel records are unproxied; this applies to the proxied tunnel hostname.)

**Verify before moving on:** open https://www.shopyor.com and https://shopyor.com — both
should load exactly as before.

---

## Phase 2 — Install and run the Cloudflare Tunnel on your PC

This publishes `http://localhost:8000` at `https://voice.shopyor.com`.

```powershell
# 1. Install cloudflared
winget install --id Cloudflare.cloudflared -e

# (open a new terminal so PATH refreshes, then:)
cloudflared --version

# 2. Authenticate — opens a browser; pick the shopyor.com zone
cloudflared tunnel login

# 3. Create a named tunnel (stable; credentials saved under ~/.cloudflared)
cloudflared tunnel create voice-clone

# 4. Route the hostname to the tunnel (creates a proxied CNAME in Cloudflare)
cloudflared tunnel route dns voice-clone voice.shopyor.com
```

Create a config file at `C:\Users\RBTG\.cloudflared\config.yml`:

```yaml
tunnel: voice-clone
credentials-file: C:\Users\RBTG\.cloudflared\<TUNNEL-ID>.json

ingress:
  - hostname: voice.shopyor.com
    service: http://localhost:8000
  - service: http_status:404
```

(Replace `<TUNNEL-ID>` with the UUID printed by `tunnel create`.)

**Run it** (in its own terminal, with the backend already running):

```powershell
cloudflared tunnel run voice-clone
```

Test from anywhere: `https://voice.shopyor.com/health` should return the GPU JSON.

**Make it permanent (start on boot)** — so you don't run it by hand each time:

```powershell
# Run as Administrator
cloudflared service install
```

This installs `cloudflared` as a Windows service using your `config.yml`. Pair it with
launching the backend on boot (Task Scheduler running `start.ps1`) and the feature is up
whenever your PC is.

---

## Phase 3 — Wire it into your Next.js site (shopyor.com on Vercel)

The reference implementation is in [`nextjs-proxy-example/`](nextjs-proxy-example/). Copy
these into your shopyor.com repo (App Router):

- `app/api/voice/clone/route.ts`
- `app/api/voice/generate/route.ts`
- `app/api/voice/_lib.ts`
- `app/voice-clone/page.tsx`  (a demo page — adapt to your UI)

**Set environment variables in Vercel** (Project → Settings → Environment Variables):

| Name | Value | Notes |
|------|-------|-------|
| `VOICE_CLONE_URL` | `https://voice.shopyor.com` | Your tunnel hostname |
| `VOICE_CLONE_API_KEY` | *(the key from `backend/.env`)* | **Server-side only — no `NEXT_PUBLIC_` prefix** |

Redeploy. The proxy routes run on Vercel's server, attach the API key, and forward the
visitor's IP via `X-Forwarded-For` (your backend has `TRUST_PROXY = True`, so per-visitor
rate limiting works).

Visit `https://www.shopyor.com/voice-clone` to test end-to-end.

---

## Phase 4 — Backend checklist before going public

- [x] **CORS** restricted to shopyor.com (via `ALLOWED_ORIGINS`, already set in `app.py`).
- [x] **API key** set in `backend/.env` (auth enabled — verify `/health` shows
      `"auth_required": true`).
- [x] **TRUST_PROXY = True** so real visitor IPs are rate-limited (not the tunnel's).
- [ ] **Rotate the API key** if it has ever been shared, and put the new one in both
      `backend/.env` and Vercel's `VOICE_CLONE_API_KEY`. Generate one:
      ```powershell
      ./venv/Scripts/python.exe -c "import secrets; print('vc_' + secrets.token_urlsafe(32))"
      ```
- [ ] *(optional hardening)* Bind the backend to localhost only — since `cloudflared`
      connects locally, you can change the last line of `backend/app.py` to
      `host="127.0.0.1"` so nothing else on your LAN can reach it.
- [ ] *(later, optional)* Rate-limit state is in-memory (resets on restart, not shared
      across workers). Fine for a single-process backend; use Redis if you scale out.

---

## Operating it day to day

To have the live feature available, **two things** must be running on your PC:
1. The backend — `./start.ps1` (or the boot task).
2. The tunnel — `cloudflared tunnel run voice-clone` (or the installed service).

Quick health check: `https://voice.shopyor.com/health`.

When your PC is off, the website feature is simply unavailable until you turn it back on.
If you later want 24/7 availability, rent an always-on GPU server, run the backend there,
and repoint `VOICE_CLONE_URL` — no website code changes needed.
```
