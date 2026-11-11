"""Read-only clients for public creator data (official APIs only; no scraping, no downloading)."""

from __future__ import annotations

import re


class SourceError(RuntimeError):
    pass


class QuotaExceeded(SourceError):
    pass


_ISO = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
_TWITCH = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def iso_duration_s(text: str | None) -> float | None:
    """YouTube ISO 8601 duration (PT1H2M3S) to seconds."""
    if not text:
        return None
    m = _ISO.fullmatch(text)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def twitch_duration_s(text: str | None) -> float | None:
    """Twitch duration (1h2m3s) to seconds."""
    if not text:
        return None
    m = _TWITCH.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(x or 0) for x in m.groups())
    return float(h * 3600 + mi * 60 + s)
