"""Shared types for the platform clients.

The clients are written against the official APIs but have only ever been exercised through
mocks: no request has been sent to YouTube, Instagram or TikTok from this code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class PlatformError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


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
        raise PlatformError(f"{what} failed: HTTP {resp.status_code} {resp.text[:300]}",
                            retryable=resp.status_code == 429 or resp.status_code >= 500)
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}
