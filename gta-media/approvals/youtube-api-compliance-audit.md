# YouTube Data API: Google Cloud project and compliance audit (draft answers)

> Draft prepared for the owner to submit **by hand**. Nothing has been submitted. No Google Cloud project exists
> for VicePulse yet.

## What was checked, and when

Checked on 10 November 2026:
- `videos.insert` reference: https://developers.google.com/youtube/v3/docs/videos/insert
- YouTube API Services audit and quota extension form: https://support.google.com/youtube/contact/yt_api_form

### Conflict to resolve before relying on either

PLAN.md says uploads from an unaudited API project are locked to private. **The current `videos.insert` page
says the opposite**: "Videos uploaded from unverified API projects are not restricted to private viewing mode",
and only notes that some uploads may default to private because of the channel's own settings. It also lists the
upload quota as **100 calls per day, 1 unit each, in a separate "Video Uploads" bucket**.

What this means: public uploads may be possible without the audit, and 10 uploads a day (5 per channel) fits the
default upload quota. The code still uploads as **private** until the owner marks the account as audited in the
database, which is the safe default. Confirm in the Cloud Console on a test upload before changing it.

**Could not verify:** whether the audit is still needed for any of our use (it is still needed for more quota),
how long reviews take now, and section 6 of the form (truncated when read).

## Draft form answers

**Request type:** Compliance audit (initial). Request quota only if the default upload bucket is not enough.

**Organisation:** {{LEGAL_ENTITY}}, {{ADDRESS}}. Website: {{WEBSITE}} (https).

**Business description (100–5,000 characters):**
> {{BRAND}} is a gaming media brand that runs two YouTube channels, VicePulse EN (English) and VicePulse BR
> (Brazilian Portuguese), with short commentary videos about Grand Theft Auto. Our internal publishing tool
> uploads the Shorts our editors have approved to our own two channels, sets their titles, descriptions and
> AI-content disclosure, and reads our own channels' analytics so we can learn what our audience likes.
> All footage is our own gameplay or footage creators have licensed to us in writing. A person reviews and
> approves every video before upload.

**Target audience:** gaming fans aged 18+. **Monetisation:** YouTube Partner Program ads on our own channels;
we do not sell ads on other people's content.

**API client name:** {{BRAND}} Publisher (must not contain "YouTube").

**Primary access URL / privacy policy / terms:** {{WEBSITE}}/publisher (login-protected), {{WEBSITE}}/privacy,
{{WEBSITE}}/terms.

**Publicly accessible?** No. Internal tool used by our own staff for our own channels.

**Demo account:** a reviewer login for the approval form, with a test channel. Prepare one before applying.

**Use case category:** Creator tools (upload) + Analytics (own channels).

**OAuth:** Yes; our staff sign in with the Google account that owns each channel. Scopes:
`https://www.googleapis.com/auth/youtube.upload`, `https://www.googleapis.com/auth/youtube` (to delete our own
videos on takedown), `https://www.googleapis.com/auth/yt-analytics.readonly`.

**Endpoints:** `videos.insert`, `videos.delete`, YouTube Analytics `reports.query`; for prospecting,
`search.list` (type=channel) once a week, about 4 calls (400 units).

**Expected volume:** fewer than 1,000 requests per day.

**Evidence to attach:** screenshots of the privacy policy page, the homepage footer linking to it, the terms page,
the OAuth consent screen, and a screencast of approve → upload → video visible as private in YouTube Studio.

## YouTube rules that also apply (from PLAN.md research, still to re-check at submission)

- Reused and "inauthentic" content rules: every Short needs our own commentary and editing; never upload
  someone else's clip with only a credit.
- Synthetic voice must set `status.containsSyntheticMedia=true` (the code does this when the voice mode is
  `synthetic`).
- `selfDeclaredMadeForKids=false` on every upload (GTA is 18+).
