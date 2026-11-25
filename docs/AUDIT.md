# Audit of AUTOPILOT 1.0 and changes made

Reproduced bugs in the original code are marked **[confirmed]**; the rest are gaps against the requirements.

## Defects fixed
1. **[confirmed]** Login page was unstyled: `/static/*` and `/manifest.json` were behind the login redirect.
2. **[confirmed]** The session cookie contained the raw password. Now an HMAC-signed, expiring token bound to the password.
3. **[confirmed]** A non-ASCII cookie/header value crashed password comparison (HTTP 500). Comparison is now byte-safe.
4. **[confirmed]** A failed login showed no error message. Now shown, with a 5-attempt / 60 s lockout.
5. **[confirmed]** Tests wrote to the real `data/` folder. Tests now use temp directories (app factory).
6. Service worker was registered at `/static/sw.js`, so its scope never covered the app. Now served from `/sw.js`, caches static files only, never API data.
7. Feed fetching had no timeout, size cap or SSRF protection. Now guarded on every redirect hop.
8. `uvicorn[standard]` pulls `uvloop`/`watchfiles`, which commonly fail to install on Termux. Removed.
9. Clip rendering blocked the HTTP request for up to 15 minutes. Now a background job with progress.
10. Source video was deleted after one render and clip positions were blind (no duration check). Sources persist; ranges are validated.
11. Manifest lacked PNG icons (required for Android install). Added.

## Found by the new tests while building (all fixed)
- Brief parser: a range like `$150-200/month` lost its upper bound and unit; "30 second clips" was read as 3 clips; "Pay a $20 registration fee" was not flagged.
- Highlight ranking gave tied values an artificial order, which drowned out transcript signals; cues are now weighted by overlap.
- Audio analysis windows were 0.36 s, not 1 s (resampling happened after the filter graph).
- Configured import folders were not symlink-resolved (Termux `~/storage/*` are symlinks) so imports were wrongly refused.
- `replaceChildren()` does not flatten arrays: lists rendered as `[object HTMLDivElement]` in four tabs (caught by the browser-level test).
- An approved-but-unsent manual message disappeared from the action list, hiding its Copy / "I sent it" buttons.

## Kept
All 1.0 endpoints and behaviours remain (`/api/clips` still renders synchronously; project/payment forms; feeds; scan). A 1.0 database is upgraded in place, tested with seeded data.

## Not done (see README for the honest list)
Native APK; speech-to-text; animated captions; face tracking; direct Upwork/Fiverr/PayPal integrations; live-Stripe verification; testing on a physical phone.
