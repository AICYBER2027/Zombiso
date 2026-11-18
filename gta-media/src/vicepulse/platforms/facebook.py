"""Facebook Reels on a Facebook Page via the Graph API (video_reels, resumable upload).

Flow (Meta's documented Reels publishing API):
  1. POST /{page_id}/video_reels upload_phase=start            -> video_id, upload_url
  2. POST rupload.facebook.com/video-upload/{ver}/{video_id}    (the file bytes, offset 0)
  3. POST /{page_id}/video_reels upload_phase=finish, video_state=PUBLISHED or DRAFT, description
  4. GET  /{video_id}?fields=status,permalink_url                (processing / publishing state)

Needs a Page access token with pages_show_list, pages_read_engagement and pages_manage_posts. Until the owner
marks the account audited, Reels are uploaded as DRAFT (visible only in Meta Business Suite), the same idea as
YouTube's private uploads. Only ever exercised through fakes: no request has been sent to Facebook from this code.
"""

from __future__ import annotations

import os

import requests

from .base import PlatformError, PublishRequest, PublishResult, check

GRAPH = os.environ.get("FACEBOOK_GRAPH_BASE", "https://graph.facebook.com")
RUPLOAD = os.environ.get("FACEBOOK_RUPLOAD_BASE", "https://rupload.facebook.com/video-upload")
VERSION = os.environ.get("FACEBOOK_GRAPH_VERSION", "v24.0")
MIN_S, MAX_S = 3, 90            # Facebook Reels length limits for the publishing API


class FacebookPublisher:
    platform = "facebook"

    def __init__(self, *, page_id: str, access_token: str, audited: bool = False,
                 session: requests.Session | None = None):
        if not (page_id and access_token):
            raise PlatformError("Facebook Page credentials are not configured")
        self.page_id, self.token, self.audited = page_id, access_token, audited
        self.http = session or requests.Session()
        self.base = f"{GRAPH.rstrip('/')}/{VERSION}"

    def _p(self, **extra) -> dict:
        return {"access_token": self.token, **extra}

    def publish(self, req: PublishRequest) -> PublishResult:
        duration = (req.options or {}).get("duration_s")
        if duration is not None and not (MIN_S <= float(duration) <= MAX_S):
            raise PlatformError(f"Facebook Reels must be {MIN_S}-{MAX_S} s long (this one is {duration:.1f} s)")
        start = check(self.http.post(f"{self.base}/{self.page_id}/video_reels", timeout=60,
                                     params=self._p(upload_phase="start")), "reel upload start")
        video_id = start.get("video_id")
        if not video_id:
            raise PlatformError("Facebook did not return a video id")
        size = req.file_path.stat().st_size
        upload_url = start.get("upload_url") or f"{RUPLOAD.rstrip('/')}/{VERSION}/{video_id}"
        with open(req.file_path, "rb") as f:
            done = check(self.http.post(upload_url, data=f, timeout=600, headers={
                "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)}), "reel upload")
        if done.get("success") is False:
            raise PlatformError("Facebook rejected the uploaded file")
        state = "PUBLISHED" if self.audited else "DRAFT"
        fin = check(self.http.post(f"{self.base}/{self.page_id}/video_reels", timeout=60, params=self._p(
            upload_phase="finish", video_id=video_id, video_state=state, description=req.caption(2200))),
            "reel finish")
        if fin.get("success") is False:
            raise PlatformError("Facebook did not accept the Reel")
        info = self.status(video_id)
        link = info.get("permalink_url")
        if link and link.startswith("/"):
            link = "https://www.facebook.com" + link
        return PublishResult(str(video_id), link, "public" if self.audited else "draft",
                             {"status": info.get("status"), "state": state})

    def status(self, video_id: str) -> dict:
        try:
            return check(self.http.get(f"{self.base}/{video_id}", timeout=30,
                                       params=self._p(fields="status,permalink_url")), "reel status")
        except PlatformError:
            return {}                      # the post went through; the status is only informational

    def metrics(self, external_id: str) -> dict:
        data = check(self.http.get(f"{self.base}/{external_id}/video_insights", timeout=60, params=self._p()),
                     "reel insights")
        vals = {m.get("name"): (m.get("values") or [{}])[0].get("value") for m in data.get("data", [])}
        social = vals.get("post_video_social_actions") or {}
        avg_ms = vals.get("post_video_avg_time_watched")
        return {"views": vals.get("blue_reels_play_count") or vals.get("fb_reels_total_plays"),
                "likes": vals.get("post_video_likes_by_reaction_type") and sum(
                    vals["post_video_likes_by_reaction_type"].values()),
                "comments": social.get("COMMENT") if isinstance(social, dict) else None,
                "shares": social.get("SHARE") if isinstance(social, dict) else None,
                "watch_time_s": None, "avg_view_pct": None,
                "avg_watch_s": avg_ms / 1000 if isinstance(avg_ms, (int, float)) else None,
                "followers_delta": vals.get("post_video_followers"), "raw": data}

    def take_down(self, external_id: str) -> bool:
        body = check(self.http.delete(f"{self.base}/{external_id}", timeout=60, params=self._p()), "delete reel")
        return bool(body.get("success", True))
