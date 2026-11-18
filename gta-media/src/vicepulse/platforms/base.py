"""Shared types for the platform clients.

The clients are written against the official APIs but have only ever been exercised through
mocks: no request has been sent to YouTube, Instagram or TikTok from this code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class PlatformError(RuntimeError):
    """kind: auth | permission | quota | rate_limit | network | rejected | config | unknown.
    retry_after: seconds to wait before trying again (when the platform says so)."""

    def __init__(self, message: str, *, retryable: bool = False, kind: str = "unknown",
                 retry_after: float | None = None):
        super().__init__(message)
        self.retryable, self.kind, self.retry_after = retryable, kind, retry_after


# Meta Graph API error codes (developers.facebook.com/docs/graph-api/guides/error-handling)
_META_AUTH = {102, 190, 463, 467}
_META_PERMISSION = {3, 10, 200, 299}
_META_RATE = {4, 17, 32, 341, 613, 80001, 80002}
_QUOTA_WORDS = ("quotaexceeded", "dailylimitexceeded", "uploadlimitexceeded", "spam_risk_too_many_posts",
                "publishing limit")


def classify(status: int, body_text: str, body: dict | None = None) -> tuple[str, bool]:
    """(kind, retryable) for a failed HTTP response from any of the platforms."""
    text = (body_text or "").lower()
    err = (body or {}).get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    if isinstance(code, int):
        if code in _META_AUTH:
            return "auth", False
        if code in _META_RATE:
            return "rate_limit", True
        if code in _META_PERMISSION:
            return "permission", False
    if any(w in text for w in _QUOTA_WORDS):
        return "quota", True
    if status == 401 or "access_token_invalid" in text or "invalid_grant" in text:
        return "auth", False
    if status == 429 or "rate_limit" in text or "ratelimitexceeded" in text:
        return "rate_limit", True
    if status == 403:
        return "permission", False
    if status >= 500:
        return "network", True
    return "rejected", False


@dataclass
class PublishRequest:
    file_path: Path
    title: str
    description: str
    hashtags: list[str]
    language: str
    public_url: str | None = None           # where the platform can fetch the file (Instagram)
    synthetic_media: bool = False            # AI-content disclosure (synthetic voice)
    options: dict[str, Any] = field(default_factory=dict)   # reviewer's per-post choices

    def caption(self, limit: int) -> str:
        text = f"{self.title}\n\n{self.description}\n\n{' '.join(self.hashtags)}".strip()
        return text[:limit]


@dataclass
class PublishResult:
    external_id: str
    url: str | None = None
    visibility: str = "private"
    raw: dict[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    platform: str

    def publish(self, req: PublishRequest) -> PublishResult: ...
    def metrics(self, external_id: str) -> dict: ...
    def take_down(self, external_id: str) -> bool:
        """Return True if removed through the API, False if a person must remove it by hand."""
        ...


def check(resp, what: str) -> dict:
    """Raise PlatformError for a failed HTTP response, else return its JSON body."""
    if resp.status_code >= 400:
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        kind, retryable = classify(resp.status_code, resp.text, body)
        retry_after = None
        try:
            retry_after = float((getattr(resp, "headers", None) or {}).get("Retry-After"))
        except (TypeError, ValueError):
            pass
        raise PlatformError(f"{what} failed: HTTP {resp.status_code} {resp.text[:300]}", retryable=retryable,
                            kind=kind, retry_after=retry_after)
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}
