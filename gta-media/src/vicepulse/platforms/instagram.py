"""Instagram Reels via the Instagram API with Instagram Login (content publishing).

Flow: create a REELS container from a public video URL -> poll until FINISHED -> media_publish.
Containers expire after 24 hours, so scheduling lives in our own database and the container
is only created when the post is due.
"""

from __future__ import annotations

import os
import time

import requests

from .base import PlatformError, PublishRequest, PublishResult, check

GRAPH = os.environ.get("INSTAGRAM_GRAPH_BASE", "https://graph.instagram.com")


class InstagramPublisher:
    platform = "instagram"

    def __init__(self, *, user_id: str, access_token: str, session: requests.Session | None = None,
                 poll_seconds: float = 60, max_polls: int = 5):
        if not (user_id and access_token):
            raise PlatformError("Instagram credentials are not configured")
        self.user_id, self.token = user_id, access_token
        self.http = session or requests.Session()
        self.poll_seconds, self.max_polls = poll_seconds, max_polls

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def remaining_quota(self) -> int | None:
        data = check(self.http.get(f"{GRAPH}/{self.user_id}/content_publishing_limit", headers=self._h(),
                                   params={"fields": "quota_usage,config"}, timeout=30), "publishing limit")
        try:
            entry = data["data"][0]
            return int(entry["config"]["quota_total"]) - int(entry["quota_usage"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def publish(self, req: PublishRequest) -> PublishResult:
        if not req.public_url or not req.public_url.startswith("https://"):
            raise PlatformError("Instagram needs the video at a public https URL")
        quota = self.remaining_quota()
        if quota is not None and quota <= 0:
            raise PlatformError("Instagram 24-hour publishing limit reached", retryable=True)
        container = check(self.http.post(f"{GRAPH}/{self.user_id}/media", headers=self._h(), timeout=60, data={
            "media_type": "REELS", "video_url": req.public_url, "caption": req.caption(2200),
            "share_to_feed": "true"}), "create container")["id"]
        for _ in range(self.max_polls):
            status = check(self.http.get(f"{GRAPH}/{container}", headers=self._h(),
                                         params={"fields": "status_code"}, timeout=30), "container status")
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PlatformError(f"Instagram container {code}")
            time.sleep(self.poll_seconds)
        else:
            raise PlatformError("Instagram container not ready in time", retryable=True)
        media_id = check(self.http.post(f"{GRAPH}/{self.user_id}/media_publish", headers=self._h(), timeout=60,
                                        data={"creation_id": container}), "media_publish")["id"]
        link = check(self.http.get(f"{GRAPH}/{media_id}", headers=self._h(), params={"fields": "permalink"},
                                   timeout=30), "permalink").get("permalink")
        return PublishResult(media_id, link, "public", {"container": container})

    def metrics(self, external_id: str) -> dict:
        data = check(self.http.get(f"{GRAPH}/{external_id}/insights", headers=self._h(), timeout=60, params={
            "metric": "views,likes,comments,shares,saved,reach,ig_reels_avg_watch_time"}), "insights")
        vals = {m["name"]: (m.get("values") or [{}])[0].get("value") for m in data.get("data", [])}
        return {"views": vals.get("views"), "likes": vals.get("likes"), "comments": vals.get("comments"),
                "shares": vals.get("shares"), "watch_time_s": None, "avg_view_pct": None,
                "followers_delta": None, "raw": data}

    def take_down(self, external_id: str) -> bool:
        # The Instagram API has no endpoint for deleting published media as far as we could verify.
        # A person must delete the Reel in the Instagram app; the takedown workflow alerts them.
        return False
