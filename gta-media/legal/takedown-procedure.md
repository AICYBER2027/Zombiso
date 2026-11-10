# {{BRAND}} Takedown Procedure

> **DRAFT. NOT LEGAL ADVICE. Must be reviewed by a lawyer before publication.**
> In particular: whether to register a DMCA agent (US) and how Brazil's Marco Civil da Internet (Lei 12.965/2014)
> applies to a channel operator rather than a platform.

This is how {{BRAND}} handles requests to remove videos. The public part (sections 1–3) goes on the website;
section 4 is the internal runbook.

## 1. How to ask us to remove something

Email **{{CONTACT_EMAIL}}** with:
1. your name and contact details;
2. the link(s) to the {{BRAND}} video(s);
3. what you own or represent (for example "my gameplay recording", "this music track") and evidence;
4. a statement that you believe in good faith the use is not authorised;
5. a statement that the information is accurate and, if you act for someone else, that you are authorised to;
6. your signature (typed name is fine).

You can also use the platform's own copyright form (YouTube, Instagram, TikTok). Either way we act on it.

## 2. What we do and how fast

| Request | Our target |
|---|---|
| Rights holder notice with the details above | Remove from all three platforms within **24 hours** of receipt while we look into it |
| Creator revokes their footage licence | Stop new posts immediately; remove existing posts within the time in their licence |
| Rockstar Games / Take-Two request | Remove within **24 hours**, no questions |
| Platform strike or claim | Remove the clip on every platform, not only the one that flagged it |
| Personal data in a video | Remove or blur within **72 hours** |

We reply to confirm what we removed.

## 3. If you think we removed something by mistake

Reply to our confirmation email with your reasons. We will look at it again; we may restore it if the claim was
wrong and the law allows.

## 4. Internal runbook

1. **Log it.** Add the request to the takedown log (date, who, what, links).
2. **Remove.** In n8n run workflow **"VicePulse 09 - Takedown"** (form) with the clip id, or the source id if all
   clips from a recording must go. Command-line equivalent:
   `vicepulse takedown --clip <id> --reason "<reason>"` or `--source <id>`.
   - This cancels every scheduled post, deletes YouTube videos through the API, marks the clip/source as
     taken down / blocked so it can never be selected again, and lists posts to delete by hand.
3. **Manual removals.** Instagram and TikTok posts must be deleted in each app by hand (their APIs have no
   delete endpoint we could verify). The workflow sends the list to Telegram. Delete them and reply in the thread.
4. **Licence revoked?** Run `vicepulse rights-revoke --id <ledger id> --reason "<reason>"`; this takes down every
   clip from every source under that licence.
5. **Confirm** to the requester by email with what was removed and when.
6. **Review.** If the pipeline let the clip through (rights gate, spoiler filter), file a fix before the next
   daily run.
