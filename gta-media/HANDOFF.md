# VicePulse - handoff for a session running on the owner's PC

Read this first, then `README.md`. Work in `C:\Users\W11\Projects\Zombiso\gta-media` on branch
`claude/gta-media-plan`. Commit and push to that branch only; never open a pull request.

## Ground rules from the owner
- Live publishing stays **off** (`VICEPULSE_ALLOW_LIVE_PUBLISH=0`) until the owner says otherwise.
- AI stays in **mock** mode (`LLM_MODE=mock`); no paid APIs, no purchases, no account creation.
- Never print, log or commit secrets (API keys, OAuth secrets, access tokens). `.env` is git-ignored.
- Do not delete or reset data, Docker volumes, the database or credentials. Ask before anything destructive.
- Only footage the owner owns or is licensed to use (own recordings, CC BY/CC0, or a creator's written
  permission recorded with evidence). No downloading from YouTube/Twitch.
- The owner prefers short, one-step-at-a-time guidance in plain words.

## What runs on this PC (Docker Desktop, `docker compose`)
postgres, redis, n8n + n8n-worker (queue mode), `vicepulse` (Python worker + FFmpeg + local Whisper, built
with `$env:VICEPULSE_BUILD_WHISPER="1"`), `review` (http://localhost:8090, bound to 127.0.0.1).
Rebuild after `git pull`:
```powershell
$env:VICEPULSE_BUILD_WHISPER = "1"
docker compose build vicepulse review
docker compose up -d vicepulse review
```
The worker image holds its own copy of the code, so it must be rebuilt after every pull.

## Done and verified on this PC
- Creator discovery (YouTube Data API + Twitch Helix), language filtering, scoring with star power.
- Highlight workflow: `scripts\make-highlight.ps1` (pick a video, one rights question, best 30-60 s,
  Whisper word-timed subtitles, 1080x1920, saved to `Videos\VicePulse`, never published). Tested EN and BR.
- Speech-aware moment picking (reactions/laughter boost), duplicate detection on real speech.
- Review page: approve/reject clips, authorize creators with proof links, Accounts tab.
- YouTube: both channels connected and verified with `docker compose exec -T vicepulse vicepulse accounts-check`
  (EN `UC71cgAhbWrXoGnWMkSm4fmQ` @vicepulse-en, BR `UCLVq4m7ple1QBEAG1OQFtLQ` @vicepulse-brasil).

## In progress: Instagram (EN @vicepulse.en, BR @vicepulse.brasil)
Meta app "VicePulse" uses **Instagram API (Unified Login)** with **system-user tokens** from the dashboard's
"Generate Token" button (Redirect/Login URLs in Meta are placeholders on example.com because Meta refuses
localhost; they are not used with system-user tokens).
The tokens are `EAA...`; the Instagram accounts are assigned to the system user directly (no Facebook Pages).
Next steps:
1. Make sure the PC is on the latest commit (`git log --oneline -1`), rebuild (above).
2. http://localhost:8090/accounts -> Instagram -> "Helper for system-user tokens": the owner pastes the
   Meta app ID and app secret (App settings > Basic) and saves. Do not ask for the secret in chat.
3. "ALL CHANNELS" row: the owner pastes the token once -> Check and save.
4. Verify: `docker compose exec -T vicepulse vicepulse accounts-check` -> instagram en/br
   `matches_expected: true`, `publish_permission: true`, `live_publishing: off`.
If validation fails, the page lists each route tried with Meta's message (no token). The logic is in
`src/vicepulse/accounts.py` (`find_instagram_account`, `instagram_save_token`); tests in
`tests/test_review_page.py`.

## Added 2026-11-18 (overnight session) - see `docs/PLATFORM.md` and `docs/EDIT_STYLES.md`
- Four edit styles incl. Streamer Pro, creator frame styles (gradient/solid/neon/minimal), automatic facecam.
- Website (EN + PT-BR) with creator accounts and dashboard, local only (`--profile web`, 127.0.0.1:8091).
- Creator clients: branding, preferences, permission records verified by staff, deliveries, plans, no payments.
- Facebook Reels client, safer retries, duplicate guard, rights scopes (migration 006), Kick live status.
- The running containers still have the OLD code. After the next rebuild run `vicepulse migrate` (005 + 006).

## After Instagram
- TikTok connection (developer app; private posts only until TikTok approves the app).
- Public media URL for Instagram/TikTok uploads, then analytics feedback (P4).
- Optional: free Piper narrator voice.

## Tests
`python -m pytest -q` from `gta-media` (needs PostgreSQL binaries or `TEST_DATABASE_URL`; the full suite takes
~25 minutes; last full run: 132 passed).
