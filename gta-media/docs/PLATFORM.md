# VicePulse platform: website, creator clients, publishing, sources (added 2026-11-18)

Everything here runs on the owner's PC. Nothing is public, nothing is published, no payment is possible.

## 1. Website (`src/vicepulse/web/`)

| Page | EN | PT-BR |
|---|---|---|
| Home: hero, animated previews of the four styles (pure CSS, original shapes), how it works, platforms | `/en/` | `/pt/` |
| Pricing (plans from the database; the static export uses the same values) | `/en/pricing` | `/pt/pricing` |
| Privacy policy and terms (the drafts in `legal/`, marked as drafts) | `/en/privacy`, `/en/terms` | same, with a note that the Brazilian (LGPD) version is still to be written |
| Sign-up / sign-in | `/en/signup`, `/en/login` | `/pt/...` |
| Dashboard: overview, clips (preview, download, approve, request changes), branding (colours, frame style, logo), editing preferences, permissions, analytics, plan, account | `/en/dashboard/...` | `/pt/dashboard/...` |
| Staff: check creators' permission requests | `/en/dashboard/staff` (staff only) | |

Run it locally: `docker compose --profile web up -d web` (after a rebuild) and open http://localhost:8091,
or `vicepulse web` outside Docker. Create a staff login: `vicepulse staff-create --email you@example.com`
(the password is typed into a hidden prompt or read from `VICEPULSE_STAFF_PASSWORD`).

Static public pages for a free host (needed as the privacy-policy URL for TikTok and Meta app reviews):
`vicepulse web-build --out site-dist --app-url https://<where the dashboard will live>`. This only writes files.
Uploading them (GitHub Pages, Cloudflare Pages, Netlify...) needs the owner's approval.

Security: PBKDF2 passwords, hashed session tokens in an HttpOnly SameSite cookie, a CSRF token per session on
every form, an Origin check, sign-in lockout after 5 failures, strict security headers (no scripts allowed at
all), per-client isolation on every page and file, uploaded logos re-encoded as PNG, 6 MB upload limit.

## 2. Creator clients (`src/vicepulse/clients.py`, migration 006)

- Accounts: owner / editor / viewer roles, plus VicePulse staff who see every client.
- Branding: name, handle, monogram, four colours, logo, and the **facecam frame**: style gradient / solid / neon /
  minimal, corner radius, thickness. The edit styles use it (`graphics.Assets` reads `brand.frame`).
- Editing preferences: style (limited by the plan), framing, captions and their language, platforms, facecam
  (auto / none / x,y,w,h).
- Permission records: the creator says what they allow (clip creation, posting on VicePulse channels, posting on
  their own channels, monetisation, branding, live feed). **A record is only a request.** Staff check the
  evidence and verify it; that writes or updates the client's rights-ledger row with those `scopes`. Withdrawing
  a permission removes the scope at once (no scopes left: the row is revoked).
- Client renders: `vicepulse client-render --client <slug> --file <video> --start 40 --end 75 --title "..."`.
  Needs the verified `clip_creation` scope, respects the plan (style, size, clips per month), runs the quality
  check, and delivers to the dashboard. It adds nothing to the VicePulse sources, clips or publishing queue.
  `vicepulse client-deliver` puts an already-finished file in a client's dashboard.
- Plans (placeholder prices, early access): Trial (free, 5 clips, Clean), Starter $49 (30), Pro $149 (120,
  all four styles, branding, 1 live channel), Studio $399 (400, 3 live channels, publishing, analytics).
- Billing architecture: `subscriptions` table (provider none / manual / stripe / mercadopago) and a
  `payment_provider()` hook. Only "none" exists: any checkout raises "payments are not switched on". A paid plan
  request is recorded for staff to confirm by hand (`staff_set_plan`, provider "manual").

## 3. Rights-ledger scopes

`rights_ledger.scopes` (NULL for entries made before scopes existed: they keep their old meaning of clip
creation, VicePulse-channel posting and monetisation). Gate 1 (ingest) needs `clip_creation`; gate 2 (before each
post) needs `vicepulse_channel_publishing`, and `monetisation` when the channel is monetised.

## 4. Publishing (`publish.py`, `platforms/`)

| Platform | State |
|---|---|
| YouTube Shorts | Connected for EN and BR (earlier). Uploads private until the API project passes Google's audit. |
| Instagram Reels | Code ready; still needs the publish permission (token with `instagram_business_content_publish`) and a public https URL for the video. |
| Facebook Reels (new) | Client written to Meta's Reels API (start, upload, finish as DRAFT until marked audited, status). Uses the Instagram render. Needs a Facebook Page per channel and a Page token: `vicepulse facebook-connect --channel en`. The test rights entries do not list Facebook, so Facebook jobs are blocked until a licence includes it. |
| TikTok | Code ready; needs the TikTok app approved (until then: private SELF_ONLY posts only). |

Improvements: error kinds (auth, permission, quota, rate_limit, network, rejected, duplicate), Retry-After
honoured, retries spaced 15 / 30 / 60 min (quota: 6 h), at most 4 tries; a connection dropped during an upload
is never retried automatically (the post may exist). The same file is never posted twice to the same account.
Each job keeps an attempt log. `vicepulse publish-queue` shows counts and errors; `vicepulse publish-requeue
--job N --reason "..."` puts a checked job back. Live publishing stays off (`VICEPULSE_ALLOW_LIVE_PUBLISH=0`).

## 5. Sources

- Twitch: official Create Clip + Get Clips Download (earlier).
- YouTube and Kick streamers: second OBS output to the MediaMTX receiver (earlier). Opening it to the internet
  needs the owner's approval.
- Kick (new): official public API for live status (`vicepulse kick-status --slug a,b`, app token from
  `KICK_CLIENT_ID/SECRET`) and webhook signature verification. Kick has no official clip or VOD download.

## 6. Facecam

Pipeline renders now detect the facecam automatically (`VICEPULSE_FACECAM=auto`, default). The worker image
installs OpenCV headless for this; without it the clip renders as if there were no facecam.
