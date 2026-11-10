# Meta (Instagram) app setup (draft)

> Draft for the owner to do **by hand**. Nothing has been created. No Meta developer app exists for VicePulse yet.

## What was checked, and when

Checked on 10 November 2026: Instagram content publishing docs,
https://developers.facebook.com/docs/instagram-platform/content-publishing

Verified there:
- With **Instagram API with Instagram Login** (what the code uses, on `graph.instagram.com`), the permissions
  are `instagram_business_basic` and `instagram_business_content_publish`.
- Reels: `POST /<IG_ID>/media` with `media_type=REELS` and `video_url`, then `POST /<IG_ID>/media_publish`
  with `creation_id`. Container status: `GET /<container>?fields=status_code`; `FINISHED` means ready; Meta
  suggests checking once a minute for up to 5 minutes.
- Limit: "100 API-published posts within a 24-hour moving period", checked with
  `GET /<IG_ID>/content_publishing_limit`. (The same page also says 50 in its carousel section; the code reads the
  account's own limit endpoint instead of trusting either number.)
- Containers not published within 24 hours expire.

**Could not verify:** whether App Review is needed when the app only publishes to accounts owned by the app's own
admins (the page does not say; Meta has historically allowed this in development mode); the `caption` and
`share_to_feed` parameters for Reels (not on the page read; the code sends both); the current Graph API version;
whether published media can be deleted through the API (the code assumes not and asks a person to delete).

## Steps for the owner

1. Convert both Instagram accounts (VicePulse EN, VicePulse BR) to **Professional** accounts (Creator or
   Business).
2. Create a Meta developer account and a **Business** type app named "{{BRAND}} Publisher".
3. Add the **Instagram** product, choose "API setup with Instagram login", add both Instagram accounts as
   testers / roles on the app, and generate long-lived tokens for each.
4. Put the user IDs and tokens in the server's `.env` only (`INSTAGRAM_USER_ID_EN`, `INSTAGRAM_ACCESS_TOKEN_EN`,
   `..._BR`). Long-lived tokens expire after about 60 days; the health check should warn before then
   (**token expiry alerts are not built yet**).
5. Make the renders reachable at a public https URL (`VICEPULSE_PUBLIC_MEDIA_BASE_URL`), e.g. a storage
   bucket with short-lived or unguessable paths. Instagram fetches the video from there.
6. Fill the app's privacy policy URL, terms URL and data deletion instructions URL (drafts in `legal/`).
7. If Meta asks for App Review for `instagram_business_content_publish`, use this description:

> {{BRAND}} Publisher publishes short gaming commentary videos to the two Instagram professional accounts owned
> by {{BRAND}}. A team member approves each Reel before it is published. The app also reads insights for those
> two accounts only. It does not access any other user's account or data.

Screencast for review: approval → Reel appears on the test account → insights screen.
