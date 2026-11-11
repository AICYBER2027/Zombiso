# Owner checklist: what only a person can do

Nothing on this list has been done. The code is ready to use each item once it exists.

## Decisions (defaults used in the code, all changeable)

| Decision | Default used | Where to change |
|---|---|---|
| Brand | VicePulse EN / VicePulse BR | `config/channels.json`, `{{BRAND}}` in `legal/` |
| Commentary voice | Owner records each script (`VICEPULSE_VOICE_MODE=own`); clips render with captions only until a recording exists | `.env` |
| n8n | Self-hosted, queue mode | `docker-compose.yml` |
| Languages | English (EN channel) and Brazilian Portuguese (BR channel), stored per clip | `config/channels.json` |
| Clips per day | 5 per channel (10 a day, 30 posts across 3 platforms) | `config/channels.json` |
| Claude models | `claude-opus-5-5` (selection, commentary, final check), `claude-haiku-5-5` (triage) | `.env` |
| Monetised channels | Yes: every licence must allow monetisation | `VICEPULSE_MONETISED` |

## Accounts and paperwork (Phase 0)

- [ ] Form or choose the legal entity that will own the channels; fill `{{LEGAL_ENTITY}}` etc. in `legal/`.
- [ ] Have a lawyer review the four documents in `legal/` (budget $300–$800), including Brazilian Portuguese
      versions for the BR side.
- [ ] Register a domain and publish the privacy policy, terms and takedown page.
- [ ] Create six channel accounts by hand: YouTube, Instagram (Professional) and TikTok, for EN and for BR.
- [ ] Google Cloud project + OAuth consent screen; connect both YouTube channels; get refresh tokens.
      Answers in `approvals/youtube-api-compliance-audit.md`.
- [ ] Meta developer app + Instagram Login; long-lived tokens for both accounts.
      Steps in `approvals/meta-app-setup.md`.
- [ ] TikTok developer app, Login Kit + Content Posting API, then the audit.
      Answers in `approvals/tiktok-content-posting-audit.md`.
- [ ] Anthropic API key with a spend limit set in the Console, once you approve paid calls.
- [ ] Telegram bot + review chat for alerts and approvals.

## Creator discovery

- [ ] YouTube Data API key (same Google Cloud project as uploads; restrict the key to YouTube Data API v3) and
      a Twitch application (client id + secret). Put them in `.env`; rebuild/restart the worker.
- [ ] Review creators in `vicepulse creators`. For each creator you want to clip, get documented permission
      (a signed licence, the creator's written OK, or their published clipping-programme terms), save it
      somewhere with an https link, then run `vicepulse creator-review ... --decision authorize`.
- [ ] Arrange how each authorized creator's footage reaches you as files. Downloading from YouTube or
      Twitch is not allowed by their terms, and VicePulse has no downloader.

## Recording

- [ ] Console (PS5 or Xbox Series X/S for GTA 6) and capture card. Record GTA 5 now for testing.
- [ ] Turn in-game radio off when recording (licensed music causes copyright claims).
- [ ] Save recordings into the watched folder; storage sync calls workflow 01 for each new file.
- [ ] For each recording, one rights ledger row of type `own` (one row can cover all own recordings):
      `vicepulse rights-add --json '{"owner_name": "VicePulse", "licence_type": "own", "platforms": ["youtube","instagram","tiktok"], "starts_on": "2026-11-10", "monetisation_allowed": true, "derivative_edits_allowed": true}'`
      then `vicepulse rights-verify --id <id> --by "<your name>"`.
- [ ] Record your voice for each approved script as `<VOICE_DIR>/<clip id>.wav` (about two minutes a day).

## Before anything goes live

- [ ] Server, storage (with a public https path for Instagram), and the docker-compose stack running.
- [ ] Import the ten workflows from `n8n/workflows/`, attach the "VicePulse worker" and "VicePulse Telegram"
      credentials (plus "YouTube Data API key", a Query Auth credential with parameter name `key`, for
      workflows 07 and 08), replace the `REPLACE_WITH_TELEGRAM_REVIEW_CHAT_ID` and
      `REPLACE_WITH_ROCKSTAR_NEWSWIRE_FEED_URL` placeholders in the nodes, then activate them one by one.
      Telegram nodes are imported **disabled**; enable them (select node, press D) after creating the
      "VicePulse Telegram" credential and filling in the chat id.
      (The workflows avoid n8n Variables, which are a paid n8n feature.)
- [ ] Run a week with `LLM_MODE=anthropic` and `VICEPULSE_ALLOW_LIVE_PUBLISH=0` to check Claude's picks and the
      cost (estimate in PLAN.md section 4).
- [ ] Set `VICEPULSE_ALLOW_LIVE_PUBLISH=1` and enable one account at a time
      (`UPDATE platform_accounts SET enabled = TRUE WHERE ...`).
- [ ] Mark an account `audited = TRUE` only after that platform's audit passes.
- [ ] Remove the approval step (`VICEPULSE_REQUIRE_APPROVAL=0`) only after the go/no-go checks in PLAN.md.
      TikTok will still need a person's per-post choices.

## Built but needing your eyes

- Rockstar / Take-Two's video policy for GTA 6 at launch: update `config/games.json` block tags if needed.
- Read Instagram's and TikTok's in-app deletion steps: takedowns on those two platforms are manual.
