"""Language identification for creator channels, from metadata and text. No external services.

Signals, strongest first:
  1. platform language fields (YouTube defaultAudioLanguage / defaultLanguage, Twitch stream language)
  2. the creator's country (BR -> pt, MX/AR/ES/... -> es, ...)
  3. words in titles and descriptions: function words and characters that separate English, Portuguese
     and Spanish (ã õ ç vs ñ ¿ ¡), plus non-Latin scripts

A channel only counts as *confirmed* off-target with enough evidence; thin or mixed evidence is "unknown".
"""

from __future__ import annotations

import re
from collections import Counter

TARGETS = ("en", "pt")

# Function words that separate the languages. Words shared between Portuguese and Spanish ("que", "para",
# "no") and English gaming loanwords ("gameplay", "live", "stream") are left out on purpose.
STOPWORDS = {
    "en": set("the and you your this that with are was what just is it of to on we my how why when will can they "
              "be have not all new first best every now finally i'm don't it's we're there their than been".split()),
    "pt": set("não você vocês está estão com uma isso muito mais pra tá também então porque quando agora nunca "
              "ainda jogo jogando melhor primeiro novo nova do da dos das nos nas é eu ele ela tudo isso aqui "
              "gente vamos foi tem já sobre até seu sua meu minha".split()),
    "es": set("el los las del una es con por pero muy más está están también entonces porque cuando ahora nunca "
              "todavía juego jugando mejor nuevo nueva y lo le se mi tu yo él ella hay todo esto aquí vamos fue "
              "tiene ya sobre hasta su mis nuestro".split()),
    "fr": set("les des est une pour avec dans pas qui sur c'est je vous nous très mais aussi".split()),
    "de": set("der die das und ist nicht mit ich wir ein eine auf für auch noch sehr".split()),
    "it": set("il gli della è non per sono questo molto anche delle nella".split()),
    "tr": set("ve bir bu için çok ile ne de da".split()),
    "id": set("dan yang ini itu tidak dengan untuk ada aku kita".split()),
}
CHAR_HINTS = {
    "pt": re.compile(r"[ãõ]|ção|ções|ões|\bao vivo\b|\bvocês?\b|\bdiários?\b|\bnovidades\b|\bcriadores\b"),
    "es": re.compile(r"[ñ¿¡]|ción\b|ciones\b|\ben vivo\b|\btráiler\b|\bustedes\b|\bdiarios?\b|\bnovedades\b"
                     r"|\bjuegos?\b|\bnoticias\b|\bvídeos? diarios\b"),
}
SCRIPTS = [("ru", re.compile(r"[Ѐ-ӿ]")), ("ar", re.compile(r"[؀-ۿ]")),
           ("ko", re.compile(r"[가-힯]")), ("ja", re.compile(r"[぀-ヿ]")),
           ("zh", re.compile(r"[一-鿿]")), ("hi", re.compile(r"[ऀ-ॿ]")),
           ("th", re.compile(r"[฀-๿]"))]
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)

COUNTRY = {"BR": "pt", "PT": "pt", "AO": "pt", "MZ": "pt",
           **{c: "es" for c in "MX AR ES CO CL PE VE EC GT CU BO DO HN PY SV NI CR PA UY PR".split()},
           **{c: "en" for c in "US GB CA AU NZ IE".split()}, "FR": "fr", "DE": "de", "AT": "de", "IT": "it",
           "TR": "tr", "ID": "id", "RU": "ru", "JP": "ja", "KR": "ko", "IN": None}


def base(code: str | None) -> str | None:
    return code.split("-")[0].split("_")[0].lower() if code else None


def detect_text(text: str | None) -> tuple[str | None, float]:
    """(language, confidence 0..1) for one piece of text, or (None, 0) if there is not enough to go on."""
    if not text:
        return None, 0.0
    low = text.lower()
    for lang, pat in SCRIPTS:
        if len(pat.findall(low)) >= 3:
            return lang, 0.9
    words = _WORD.findall(low)
    scores = Counter()
    for w in words:
        for lang, sw in STOPWORDS.items():
            if w in sw:
                scores[lang] += 1
    for lang, pat in CHAR_HINTS.items():
        scores[lang] += 1.5 * len(pat.findall(low))
    if not scores:
        return None, 0.0
    lang, top = scores.most_common(1)[0]
    total = sum(scores.values())
    if top < 1.5:          # one distinctive phrase ("ao vivo", "en vivo") is enough; one common word is not
        return None, 0.0
    return lang, round(top / total, 3)


def classify_channel(*, country: str | None = None, description: str | None = None, items: list[dict] | None = None,
                     declared: str | None = None, min_evidence: float = 6, min_share: float = 0.65) -> dict:
    """Decide a channel's language.

    items: [{"language": "es-419" | None, "text": "title + description"}]
    declared: a language we already trust (seed config, or the Twitch stream language filter)
    Returns {language, confidence, confirmed, evidence}. ``language`` is None when unknown.
    """
    votes: Counter = Counter()
    sources: Counter = Counter()

    def vote(lang, weight, source):
        if lang:
            votes[lang] += weight
            sources[f"{source}:{lang}"] += 1

    vote(base(declared), 4, "declared")
    vote(COUNTRY.get((country or "").upper()), 2, "country")
    lang, conf = detect_text(description)
    if conf >= 0.6:
        vote(lang, 2, "channel_description")
    for it in (items or [])[:40]:
        vote(base(it.get("language")), 2, "metadata")
        lang, conf = detect_text(it.get("text"))
        if conf >= 0.6:
            vote(lang, 1, "text")
    total = sum(votes.values())
    if not votes:
        return {"language": None, "confidence": 0.0, "confirmed": False, "evidence": {}}
    top, weight = votes.most_common(1)[0]
    share = weight / total
    confirmed = total >= min_evidence and share >= min_share
    # A target language only needs a plurality to be used; an off-target one must be confirmed.
    language = top if (top in TARGETS and share >= 0.5) or confirmed else None
    return {"language": language, "confidence": round(share, 3), "confirmed": confirmed and language == top,
            "evidence": {"votes": dict(votes), "sources": dict(sources), "total": total}}


def off_target(result: dict, targets: tuple[str, ...] = TARGETS) -> bool:
    """True only when a channel is confirmed to be in a language outside the targets."""
    return bool(result.get("confirmed")) and result.get("language") not in targets
