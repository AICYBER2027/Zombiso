"""YouTube Data API v3, read-only with an API key (free; costs quota units, not money).

Quota costs (Google's published table): search.list 100 units; channels.list, videos.list and
playlistItems.list 1 unit each. Every call goes through ``spend`` so a daily budget can stop
discovery before the project's quota (default 10,000 units/day) runs out.
"""

from __future__ import annotations

from typing import Callable

import requests

from . import QuotaExceeded, SourceError

API = "https://www.googleapis.com/youtube/v3"
COST = {"search": 100, "channels": 1, "videos": 1, "playlistItems": 1}


class YouTubeData:
    def __init__(self, api_key: str, *, spend: Callable[[int], None] | None = None,
                 session: requests.Session | None = None):
        if not api_key:
            raise SourceError("YOUTUBE_DATA_API_KEY is not set")
        self.key = api_key
        self.spend = spend or (lambda units: None)
        self.http = session or requests.Session()

    def _get(self, resource: str, **params) -> dict:
        self.spend(COST[resource])                     # raises QuotaExceeded before the call
        r = self.http.get(f"{API}/{resource}", params={**params, "key": self.key}, timeout=30)
        if r.status_code == 403 and "quota" in r.text.lower():
            raise QuotaExceeded("YouTube Data API quota exhausted")
        if r.status_code >= 400:
            raise SourceError(f"YouTube {resource} failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def channel_by_handle(self, handle: str) -> dict | None:
        items = self._get("channels", part="snippet,statistics,contentDetails,brandingSettings",
                          forHandle=handle.lstrip("@")).get("items") or []
        return items[0] if items else None

    def channels(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            out += self._get("channels", part="snippet,statistics,contentDetails,brandingSettings",
                             id=",".join(ids[i:i + 50]), maxResults=50).get("items") or []
        return out

    def search_videos(self, q: str, *, event_type: str | None = None, region: str | None = None,
                      language: str | None = None, published_after: str | None = None,
                      max_results: int = 50) -> list[dict]:
        params = {"part": "snippet", "type": "video", "q": q, "maxResults": max_results,
                  "order": "viewCount" if not event_type else "relevance", "safeSearch": "none"}
        if event_type:
            params["eventType"] = event_type           # live / upcoming / completed (needs type=video)
        if region:
            params["regionCode"] = region
        if language:
            params["relevanceLanguage"] = language
        if published_after:
            params["publishedAfter"] = published_after
        return self._get("search", **params).get("items") or []

    def latest_uploads(self, uploads_playlist: str, n: int = 10) -> list[str]:
        items = self._get("playlistItems", part="contentDetails", playlistId=uploads_playlist,
                          maxResults=n).get("items") or []
        return [i["contentDetails"]["videoId"] for i in items]

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            out += self._get("videos", part="snippet,statistics,liveStreamingDetails,contentDetails",
                             id=",".join(ids[i:i + 50]), maxResults=50).get("items") or []
        return out


__all__ = ["YouTubeData", "COST", "QuotaExceeded", "SourceError"]
