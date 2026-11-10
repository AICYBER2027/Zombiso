"""Rights verification. Fails closed: anything not positively proven usable is refused.

Gate 1 runs at ingest (does the source have a valid, verified ledger row at all?).
Gate 2 runs right before each publish (is the grant still valid for this platform,
this channel language, today, and for monetised use if the channel is monetised?).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone


@dataclass
class RightsDecision:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reasons": self.reasons}


def _language_covered(row_languages: list[str] | None, language: str | None) -> bool:
    if language is None:
        return True
    langs = row_languages or []
    return "*" in langs or language in langs or language.split("-")[0] in langs


def check_grant(row: dict | None, *, on: date | None = None, platform: str | None = None,
                language: str | None = None, monetised: bool = False) -> RightsDecision:
    """Pure check of one rights-ledger row. Used by both gates."""
    on = on or datetime.now(timezone.utc).date()
    reasons: list[str] = []
    if row is None:
        return RightsDecision(False, ["no rights ledger entry"])
    if row.get("verified_at") is None:
        reasons.append("rights entry not verified by a person")
    if row.get("revoked_at") is not None:
        reasons.append(f"rights revoked: {row.get('revoked_reason') or 'no reason given'}")
    if row["licence_type"] != "own" and not row.get("document_url"):
        reasons.append("no signed licence document on file")
    if row["licence_type"] == "creative_commons":
        cc = (row.get("cc_licence") or "").upper()
        if "ND" in cc.split("-"):
            reasons.append("Creative Commons NoDerivatives licence does not allow edits")
        if monetised and "NC" in cc.split("-"):
            reasons.append("Creative Commons NonCommercial licence does not allow monetised use")
    if row["licence_type"] == "written_licence" and not row.get("derivative_edits_allowed"):
        reasons.append("licence does not allow edited derivative clips")
    if row["starts_on"] > on:
        reasons.append(f"licence starts {row['starts_on']}")
    if row.get("expires_on") is not None and row["expires_on"] < on:
        reasons.append(f"licence expired {row['expires_on']}")
    if platform is not None and platform not in (row.get("platforms") or []):
        reasons.append(f"licence does not cover {platform}")
    if not _language_covered(row.get("languages"), language):
        reasons.append(f"licence does not cover language {language}")
    if monetised and not row.get("monetisation_allowed"):
        reasons.append("licence does not allow monetisation")
    return RightsDecision(not reasons, reasons)


def ingest_gate(conn, source_id: int) -> RightsDecision:
    """Gate 1: a source needs a verified, currently valid ledger row before anything is extracted."""
    src = conn.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()
    if src is None:
        return RightsDecision(False, ["unknown source"])
    row = None
    if src["rights_id"] is not None:
        row = conn.execute("SELECT * FROM rights_ledger WHERE id = %s", (src["rights_id"],)).fetchone()
    decision = check_grant(row)
    if row is not None and src["origin"] == "own_recording" and row["licence_type"] != "own":
        decision.ok = False
        decision.reasons.append("own recording linked to a non-own licence")
    if row is not None and src["origin"] == "creator_upload" and row["licence_type"] == "own":
        decision.ok = False
        decision.reasons.append("creator upload cannot use an 'own footage' ledger entry")
    return decision


def require_cleared_source(conn, source_id: int) -> dict:
    """Used by every later stage: refuse to touch footage that has not passed gate 1."""
    src = conn.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()
    if src is None or src["status"] not in ("cleared", "analysed"):
        status = src["status"] if src else "missing"
        raise PermissionError(f"source {source_id} is not cleared for use (status={status})")
    return src


def prepublish_gate(conn, clip_id: int, platform: str, *, on: date | None = None,
                    monetised: bool = False) -> RightsDecision:
    """Gate 2: re-check expiry, platform scope and language right before posting."""
    row = conn.execute(
        """SELECT r.*, s.status AS source_status, cl.language AS clip_language, cl.status AS clip_status
           FROM clips cl
           JOIN candidates c ON c.id = cl.candidate_id
           JOIN sources s ON s.id = c.source_id
           LEFT JOIN rights_ledger r ON r.id = s.rights_id
           WHERE cl.id = %s""",
        (clip_id,),
    ).fetchone()
    if row is None:
        return RightsDecision(False, ["unknown clip"])
    if row["source_status"] in ("blocked", "quarantined"):
        return RightsDecision(False, [f"source is {row['source_status']}"])
    if row["clip_status"] in ("blocked", "taken_down"):
        return RightsDecision(False, [f"clip is {row['clip_status']}"])
    grant = row if row.get("licence_type") else None
    return check_grant(grant, on=on, platform=platform, language=row["clip_language"], monetised=monetised)
