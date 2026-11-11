"""Find what a creator says publicly about clipping and reuploads.

The scanner only reports statements and where they were found. A statement is not documented
authorization: whatever it finds, the creator still needs a person to review the evidence
(``vicepulse creator-review``) before any of their footage can be used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ALLOW = [
    r"feel free to clip", r"you can clip", r"clip(?:s|ping)? (?:are|is) (?:welcome|allowed|ok|okay|fine)",
    r"clipping (?:is )?(?:allowed|welcome|encouraged)", r"go ahead and clip", r"clip (?:me|my streams?|this)\b",
    r"free to (?:repost|reupload|use) (?:my )?clips", r"pode(?:m)? clipar", r"clipes? (?:liberados?|permitidos?)",
    r"liberado (?:pra|para) clipar", r"fique à vontade para clipar",
]
DENY = [
    r"(?:do not|don't|dont|no) (?:re-?upload|repost|re-?post|clip)", r"no re-?uploads?",
    r"clip(?:s|ping)? (?:are |is )?not (?:allowed|permitted)", r"re-?uploads? (?:are |is )?not (?:allowed|permitted)",
    r"proibido (?:repostar|reupar|clipar)", r"n[aã]o (?:reupe|repost[ea]|clipe) ", r"all rights reserved",
    r"unauthori[sz]ed (?:use|re-?upload)",
]
PROGRAM = [
    r"clipping (?:program|programme|campaign|team)", r"clipper program", r"join (?:my|the|our) clippers?",
    r"programa de clipes", r"clip(?:ping)? (?:discord|server)",
]
CONDITIONS = {
    "credit_required": [r"(?:give|with|include|must) (?:me )?credit", r"credit (?:me|the channel)", r"tag me",
                        r"link (?:back|my channel|to (?:me|my channel))", r"(?:dar|com) cr[eé]ditos?"],
    "no_monetisation": [r"(?:do not|don't|no) monetiz", r"non-?commercial", r"sem monetiza"],
    "contact_first": [r"(?:email|contact|dm) (?:me|us) (?:first|before)", r"business (?:inquiries|enquiries)"],
}


_NEGATED = re.compile(r"(?:don'?t|do not|never|no|n[aã]o)\s*$", re.IGNORECASE)


@dataclass
class PolicyResult:
    verdict: str                      # unknown / stated_allow / stated_deny / program_available / conflicting
    conditions: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)


def _hits(patterns: list[str], text: str) -> list[re.Match]:
    return [m for p in patterns for m in re.finditer(p, text, flags=re.IGNORECASE)]


def _snippet(text: str, m: re.Match, pad: int = 80) -> str:
    return " ".join(text[max(0, m.start() - pad): m.end() + pad].split())


def scan(texts: list[dict]) -> PolicyResult:
    """texts: [{"source": "youtube channel description", "url": "...", "text": "..."}]"""
    allow, deny, program, conds, evidence = 0, 0, 0, set(), []
    for t in texts:
        text = t.get("text") or ""
        for kind, pats in (("allow", ALLOW), ("deny", DENY), ("program", PROGRAM)):
            for m in _hits(pats, text):
                if kind == "allow" and _NEGATED.search(text[max(0, m.start() - 14): m.start()]):
                    continue  # "don't clip me" is a refusal, already counted by DENY
                evidence.append({"kind": kind, "source": t.get("source"), "url": t.get("url"),
                                 "phrase": m.group(0), "snippet": _snippet(text, m)})
                allow += kind == "allow"
                deny += kind == "deny"
                program += kind == "program"
        for name, pats in CONDITIONS.items():
            if _hits(pats, text):
                conds.add(name)
    if deny and (allow or program):
        verdict = "conflicting"
    elif deny:
        verdict = "stated_deny"
    elif program:
        verdict = "program_available"
    elif allow:
        verdict = "stated_allow"
    else:
        verdict = "unknown"
    return PolicyResult(verdict, sorted(conds), evidence)


# How an automatic verdict changes a creator's status. 'authorized' and 'denied' are only ever set by a
# person and are never overwritten by a later scan.
STATUS_FOR_VERDICT = {"unknown": "needs_review", "conflicting": "needs_review", "stated_allow": "stated_allow",
                      "stated_deny": "stated_deny", "program_available": "program_available"}
HUMAN_STATUSES = {"authorized", "denied"}
