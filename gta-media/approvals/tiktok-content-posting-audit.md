# TikTok Content Posting API: app setup and audit (draft answers)

> Draft prepared for the owner to submit **by hand**. Nothing has been submitted. No TikTok developer account
> exists for VicePulse yet.

## What was checked, and when

Checked on 10 November 2026 against TikTok's public developer pages:
- Content Sharing Guidelines: https://developers.tiktok.com/doc/content-sharing-guidelines
- Direct Post API reference: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post

**Could not verify:** the audit application form itself (it sits behind a developer login), the exact current
wording of its questions, current review times, and the creator-info and status-fetch endpoint pages (the code
uses `/v2/post/publish/creator_info/query/` and `/v2/post/publish/status/fetch/` from memory).

## What TikTok's rules mean for VicePulse (verified)

| Rule (as published) | Effect on the plan |
|---|---|
| Unaudited apps: every post is private (`SELF_ONLY`), at most 5 users in 24 hours, and the posting account must be private | Before the audit, TikTok posting is only a test. Our client refuses anything but `SELF_ONLY` until the account row is marked audited. |
| About 15 posts per day per creator account, shared across all Direct Post apps | 5 a day per channel (EN and BR are separate accounts) fits. |
| The user must choose privacy level from the creator's own options, **with no default**; comment, Duet and Stitch must be turned on manually; consent statement linking TikTok's Music Usage Confirmation before posting; a preview; nothing is sent before explicit consent | Our approval form (n8n workflow 04) makes the reviewer pick each of these for every clip, with nothing pre-selected. The publisher refuses a TikTok post without them. **This also means TikTok posting can never be fully hands-off**: a person decides each post. |
| Commercial content disclosure toggle, off by default; "Your brand" / "Branded content"; branded content cannot be private | In the approval form. The form refuses branded content + `SELF_ONLY`. |
| Apps must not copy arbitrary content from other platforms to TikTok | Our clips are original edits with commentary, from our own or licensed footage. Say so plainly in the audit. |
| Apps must not add brand names, logos or promotional watermarks to shared content | The render adds captions only, no logo or watermark. Keep it that way. |
| Users must be able to edit preset titles and hashtags | The approval step should let the reviewer edit the TikTok caption. **Not built yet** (titles can be edited in the database); see the owner checklist. |
| Tell the user processing can take minutes; poll the status API | `TikTokPublisher.status()` exists; the n8n publisher does not yet poll it. **Not built yet.** |

## Draft answers

**App name:** {{BRAND}} Publisher (do not include "TikTok" in the name).

**App description / use case:**
> {{BRAND}} Publisher is an internal tool for the {{BRAND}} gaming channels (VicePulse EN and VicePulse BR). Our
> editors review short commentary videos about Grand Theft Auto that we produce from our own gameplay recordings
> and from footage we license in writing from creators. For each video, a team member opens the review page,
> sees which TikTok account will receive the post, picks the privacy level from the options TikTok returns for
> that account, chooses whether comments, Duet and Stitch are allowed, declares any commercial content, reads
> and accepts TikTok's Music Usage Confirmation, and only then the video is uploaded with the Direct Post API.
> We only post to our own two TikTok accounts. We never repost content copied from other platforms.

**Products / scopes:** Login Kit, Content Posting API (Direct Post). Scopes: `user.info.basic`, `video.publish`,
`video.list` (for our own posts' statistics).

**Expected users:** 2 TikTok accounts (our own). Active creators per day: 2.

**Expected posts per day:** 10 (5 per account).

**Upload method:** `FILE_UPLOAD` (our files are on our own servers; no URL ownership verification needed).

**Demo video for the audit:** record the n8n approval form flow end to end on a private test account:
creator nickname shown → privacy dropdown with nothing selected → interaction checkboxes off → commercial
disclosure off → consent text → preview → upload → "processing may take a few minutes" message → status.
**The approval form must first show the creator nickname and a video preview; those parts are not built yet.**

**Privacy policy URL:** {{WEBSITE}}/privacy  **Terms URL:** {{WEBSITE}}/terms (drafts in `legal/`).

## Before applying

1. Create the TikTok developer account with the business email, not a personal one.
2. Publish the privacy policy and terms on the website.
3. Build the missing review-page pieces listed above and record the demo on a private test account.
4. Keep both TikTok accounts private until the audit passes.
