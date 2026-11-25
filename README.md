# AUTOPILOT: clipping work assistant for one Android phone

Finds clipping jobs from feeds you add, reads client briefs, finds highlights in long videos, renders
Shorts/Reels/TikTok clips with captions, checks quality against the client's requirements, and tracks
clients, deadlines, revisions, deliveries and money. **Nothing is sent, accepted, committed, delivered or
spent without your tap on Approve.** Free: no paid API or subscription is needed for any core feature.

It is a local web app (Python + FFmpeg) shown in Chrome and installable as a home-screen app (PWA).
**It is not a native APK**: see [Android](#android-install) for why and what to do instead.

## Install on Android (Termux, free)

1. Install **Termux** from F-Droid (the Play Store build is outdated).
2. In Termux:
   ```bash
   pkg install -y git unzip
   unzip AUTOPILOT-source.zip && cd AUTOPILOT      # or git clone your repo
   bash scripts/termux-setup.sh                     # installs python, ffmpeg, deps, runs a self-check
   bash scripts/start.sh
   ```
3. Open **http://127.0.0.1:8000** in Chrome, then menu → **Add to Home screen / Install app**.
4. Optional: `bash scripts/termux-boot.sh` (with the Termux:Boot app) starts it after a reboot.

`termux-setup.sh` may take 10-20 minutes the first time because pip can compile `pydantic-core`
(a FastAPI dependency) with Rust on Android. `python -m app.doctor` verifies FFmpeg, every filter/encoder
the app needs, and does a real captioned render, so you find font/codec problems immediately.
To import big videos without uploading, run `termux-setup-storage` once and use *Studio → Phone folder path*
(e.g. `~/storage/movies/podcast.mp4`); the file is used in place, never copied or deleted.

## Daily workflow

| Tab | What you do |
|---|---|
| **Leads** | Add RSS/Atom feeds → *Scan feeds*. Leads get a 0-100 relevance score and scam warnings. Or paste any listing/brief with *Add lead*. *Analyse* reads the brief; *Draft reply* creates a message **for approval**; *Make project* starts a project. |
| **Studio** | Import a video (upload, phone path, or direct link) → optionally add an SRT/VTT transcript → *Find highlights* → preview and select → choose platform preset, framing (crop or blurred-background fit), captions, loudness → *Render*. Every clip is quality-checked automatically. |
| **Clients** | Brief → requirements (format, length, captions, size), deadline, revisions used/allowed, clips with QA status, payments, printable invoice. |
| **Approve** | Every outgoing or binding action waits here: messages, contract acceptance, price/deadline commitments, deliveries, spending. Edit the text, then Approve or Reject. |
| **Money** | Payment ledger, earnings per currency (paid / pending / expenses / net), CSV export. |

## What is implemented, and how it is verified

| Requirement | Status |
|---|---|
| 1 Find paid jobs from permitted sources | **Done:** any public RSS/Atom feed you add (SSRF-safe, timeouts, size caps, per-feed errors) + manual paste. Relevance scoring and scam red-flag detection. *Tested against a local feed server.* |
| 2 Analyse briefs | **Done, rule-based (no AI model):** platforms, format, length, count, captions, budget, deadline, links, unsupported requests, missing questions, scam flags. It only reports what the text says. |
| 3 Auto-find highlights | **Done, signal-based:** audio energy, dynamics, activity, optional scene-change density, plus transcript density/hook words if you supply SRT/VTT. Cuts snap to pauses. It does **not** understand meaning; always preview. *Tested: finds a known loud segment.* |
| 4 Shorts/Reels/TikTok/other formats | **Done:** 9:16, 1:1, 4:5, 16:9; crop or blurred-background fit; loudness normalisation; fades. Preset length limits are advisory defaults; the client's brief overrides them. |
| 5 Captions, resize, cuts | **Done:** captions are burned in from an SRT/VTT you provide (*verified pixel-wise that they appear in the lower third*). **Not included:** automatic speech-to-text, word-by-word animated captions. |
| 6 Quality + requirement check | **Done:** decodes cleanly, H.264/yuv420p, resolution, aspect, min/max length, file size, audio present and audible (LUFS), black frames, captions requirement. Caption presence comes from render settings, not OCR. |
| 7 Clients, deadlines, revisions, deliveries, earnings | **Done.** Overdue flags, revision limit warning, delivery history, per-currency earnings. |
| 8 Payment tracking / integrations | **Done:** manual ledger, CSV, invoice with your own payment link/instructions, Stripe webhook (signature-verified, idempotent). *Stripe is tested with simulated signed events only, never against live Stripe.* PayPal and marketplace payment APIs are **not** implemented. |
| 9 Approval before acting | **Done and enforced on the server**, not just in the UI. See below. |
| 10 Mobile UI | **Done:** bottom-tab single-page app, sheets, safe-area aware, no CDN. *Layout was not visually checked on a physical phone.* |

### The approval gate
- `send_message`: approved email is sent through **your** SMTP only after Approve; otherwise you copy the text and press *I sent it*.
- `accept_contract`: the only way a project can enter *in progress*; the status endpoint refuses it.
- `commitment`: price/deadline are written to the project only after Approve.
- `deliver`: the only way to reach *delivered*. Clips that failed QA are refused unless you tick an explicit override; QA is re-checked at approval time.
- `spend_money`: recorded as an expense only after Approve. The app never moves money.
Rejected or failed requests do nothing. Your own direct edits (e.g. editing a price field) are bookkeeping and apply immediately.

## Needs credentials or your action (all optional)

- **Email sending / delivery by email:** `SMTP_*` in `.env` (your own mailbox; Gmail needs an app password). Without it use the manual copy-and-send flow.
- **Stripe auto-recording:** your Stripe account, `STRIPE_WEBHOOK_SECRET`, and a public HTTPS URL pointing to `/webhooks/stripe` (a free tunnel such as Cloudflare Tunnel works but is not set up here).
- **Marketplaces (Upwork, Fiverr, etc.):** **no direct integration.** Fiverr has no public job API; Upwork's API needs approved credentials and has its own terms. Use their email alerts/RSS where permitted, or paste listings. Accepting/sending on those sites is done by you. Respect each site's terms and never scrape pages behind a login.
- **Transcripts:** provide an SRT/VTT (many clients and video platforms supply one). Offline speech-to-text is not bundled.
- **Source video links:** *Direct link* needs a direct file URL. Only process footage you own or have permission to use.

## Android install

This project **does not ship an APK**, deliberately. A real APK would need to bundle Python and FFmpeg, and an
Android SDK/Gradle toolchain was not available when this was built, so any APK would have been untested.
The supported route is Termux + the installable PWA above; Chrome creates a home-screen app from the PWA
manifest (icons and service worker are included and checked by tests). `.github/workflows/build-apk.yml` still
only prints this explanation.

## Configuration
Copy `.env.example` to `.env`. Everything is optional. The server listens on `127.0.0.1` only by default; if you
change `HOST` set a strong `APP_PASSWORD` (sessions are signed, expire after 30 days, login is rate-limited).

## Development and tests
```bash
pip install -r requirements-dev.txt         # needs ffmpeg installed
(cd tests/ui && npm install)                     # optional: enables the browser-level E2E test
python -m pytest -q                         # 96 tests, about 2.5 min
```
The suite covers auth, the brief analyser, feed scanning and SSRF protection, highlight detection, rendering
in every aspect, caption burn-in, every QA rule, the whole approval gate, Stripe signatures, database migration
from AUTOPILOT 1.0, UI/API contract checks, packaging, and a jsdom end-to-end run that clicks through the full
workflow against a real server and real FFmpeg.

## Layout
`scripts/make_icons.py` creates the PWA icons (PNG files are not stored in the Git repo) · `app/main.py` routes and auth · `video.py` FFmpeg engine · `brief.py` · `leads.py` · `approvals.py` · `money.py` ·
`jobs.py` background queue · `db.py` SQLite + migration · `static/app.js` UI · `scripts/` Termux helpers ·
`docs/AUDIT.md` what was wrong in 1.0 and what changed.
