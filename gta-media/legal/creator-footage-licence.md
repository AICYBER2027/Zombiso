# {{BRAND}} Creator Footage Licence (template)

> **DRAFT TEMPLATE. NOT LEGAL ADVICE. A lawyer must review it before anyone signs it.**
> Budgeted in PLAN.md section 4 ($300–$800). Prepare a Brazilian Portuguese version for Brazilian creators.

**Licence ID:** {{LICENCE_ID}} (matches the row in the {{BRAND}} rights ledger)

**Between**
- **Licensor:** {{CREATOR_LEGAL_NAME}}, known online as {{CREATOR_HANDLE}} ({{CREATOR_CHANNEL_URLS}}),
  {{CREATOR_ADDRESS}}, {{CREATOR_EMAIL}} ("Creator"), and
- **Licensee:** {{LEGAL_ENTITY}}, {{ADDRESS}}, operating {{BRAND}} ("{{BRAND}}").

## 1. The footage

"Footage" means the gameplay recordings the Creator uploads to {{BRAND}}'s creator portal under this licence ID,
listed (with file names and fingerprints) in Schedule A, which {{BRAND}} updates as files arrive and sends to the
Creator by email.

## 2. Creator's promises

The Creator confirms that:
1. the Creator recorded the Footage personally, or owns all rights needed to grant this licence;
2. the Footage contains **no** in-game radio or other licensed music, no third-party voices or faces without
   their written permission, and no other creator's watermark or content;
3. the Footage contains no leaked, unreleased or pre-release material and was not recorded in breach of any
   game's terms;
4. the Creator is at least 18 years old.

The Creator understands that Rockstar Games / Take-Two Interactive own the game itself and that this licence only
covers the Creator's own rights in the recording.

## 3. What {{BRAND}} may do (the grant)

The Creator grants {{BRAND}} a **non-exclusive** licence to:
- cut the Footage into short clips, crop and reframe it (including to 9:16), add commentary, captions, titles,
  graphics and music {{BRAND}} has licensed, and translate or dub that commentary (English and Brazilian
  Portuguese, and other languages listed here: {{EXTRA_LANGUAGES}});
- publish those clips on the {{BRAND}} channels on these platforms only: **[ ] YouTube [ ] Instagram [ ] TikTok
  [ ] Other: ____**;
- **monetise** those clips through the platforms' advertising and creator programmes: **[ ] Yes [ ] No**;
- keep the clips on {{BRAND}}'s systems for the rights ledger and legal records.

{{BRAND}} may **not**: sell or sublicense the Footage to anyone else, use it in paid advertising for third
parties, or present it as {{BRAND}}'s own recording.

## 4. Term

From {{START_DATE}} to {{END_DATE}} (or "until revoked" if no end date). {{BRAND}} will not publish new clips
from the Footage after the end date.

## 5. Credit

Every clip using the Footage will credit the Creator in the description as: **"{{CREDIT_TEXT}}"**
(on TikTok, where descriptions are short, in the caption text).

## 6. Payment

[ ] No fee. [ ] A fee of {{AMOUNT}} per {{UNIT}}, paid within 30 days of the end of each month.
[ ] Revenue share: {{PERCENT}}% of net platform revenue attributable to clips using the Footage, with a monthly
statement.

## 7. Revocation and takedown

1. The Creator may revoke this licence at any time by emailing {{CONTACT_EMAIL}} with the licence ID.
2. On revocation, {{BRAND}} will within **{{N_DAYS}} days**: stop scheduling new clips from the Footage, remove
   every published clip that uses it where the platform allows deletion, and confirm by email.
   ({{LAWYER: decide whether revocation is "at will" or only for cause, and whether live posts must come down or
   may stay. The pipeline supports full removal: see takedown-procedure.md.}})
3. If a platform, the game publisher, or a court asks for removal, {{BRAND}} may remove clips immediately.

## 8. Liability

Each side is responsible for its own promises. The Creator will compensate {{BRAND}} for losses caused by a
false promise in section 2, up to {{CAP}}. {{BRAND}} will compensate the Creator for use outside this licence,
up to {{CAP}}. {{LAWYER: caps and indemnities.}}

## 9. General

This is the whole agreement about the Footage. Changes must be in writing and signed by both. Governing law:
{{GOVERNING_LAW}}. Electronic signatures are valid.

---

**Signed**

Creator: ________________________ Date: __________

For {{LEGAL_ENTITY}}: ________________________ Date: __________

---

### For {{BRAND}} staff: rights ledger entry for this licence

After both have signed, add one row (`vicepulse rights-add --json '{...}'`) and then, once a person has checked the
signed PDF, `vicepulse rights-verify --id <id> --by <your name>`. The pipeline refuses footage until then.

| Ledger field | From this document |
|---|---|
| `owner_name` | Creator legal name / handle |
| `licence_type` | `written_licence` |
| `platforms` | section 3 ticks |
| `languages` | `["en", "pt-BR"]` plus any extra |
| `starts_on` / `expires_on` | section 4 |
| `monetisation_allowed` | section 3 |
| `derivative_edits_allowed` | `true` (section 3 grants edits) |
| `credit_text` | section 5 |
| `document_url` / `document_sha256` | where the signed PDF is stored, and its hash |
