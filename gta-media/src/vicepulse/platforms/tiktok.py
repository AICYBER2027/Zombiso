"""TikTok via the Content Posting API (direct post, FILE_UPLOAD).

TikTok's content sharing guidelines require a person to choose the privacy level and the
comment/duet/stitch settings for every post (no defaults) and to consent before upload.
Those choices are made in the approval step and arrive here in ``req.options``; this client
refuses to post without them. Unaudited apps can only post SELF_ONLY to private accounts.
"""

from __future__ import annotations

import requests

from .base import PlatformError, PublishRequest, PublishResult, check

API = "https://open.tiktokapis.com"
MIN_CHUNK, MAX_CHUNK = 5 * 1024 * 1024, 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
REQUIRED_OPTIONS = ("privacy_level", "allow_comment", "allow_duet", "allow_stitch", "consent_confirmed")


def chunk_plan(size: int, chunk: int = CHUNK) -> tuple[int, int]:
    """(chunk_size, total_chunk_count). Files under 5 MB go as one chunk; the last chunk takes the remainder."""
    if size < MIN_CHUNK or size <= chunk:
        return size, 1
    return chunk, size // chunk


class TikTokPublisher:
    platform = "tiktok"

    def __init__(self, *, access_token: str, audited: bool = False, session: requests.Session | None = None):
        if not access_token:
            raise PlatformError("TikTok credentials are not configured")
        self.token, self.audited = access_token, audited
        self.http = session or requests.Session()

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=UTF-8"}

    def creator_info(self) -> dict:
        return check(self.http.post(f"{API}/v2/post/publish/creator_info/query/", headers=self._h(), timeout=30),
                     "creator info").get("data", {})

    def publish(self, req: PublishRequest) -> PublishResult:
        opts = req.options or {}
        missing = [k for k in REQUIRED_OPTIONS if k not in opts]
        if missing:
            raise PlatformError(f"TikTok post needs the reviewer's choices: {', '.join(missing)}")
        if opts["consent_confirmed"] is not True:
            raise PlatformError("reviewer did not confirm TikTok's music usage consent")
        privacy = opts["privacy_level"]
        if not self.audited and privacy != "SELF_ONLY":
            raise PlatformError("unaudited TikTok app can only post SELF_ONLY")
        info = self.creator_info()
        if privacy not in info.get("privacy_level_options", []):
            raise PlatformError(f"privacy level {privacy} is not offered for this account")
        size = req.file_path.stat().st_size
        chunk_size, count = chunk_plan(size)
        post_info = {
            "title": req.caption(2200), "privacy_level": privacy,
            "disable_comment": not opts["allow_comment"] or bool(info.get("comment_disabled")),
            "disable_duet": not opts["allow_duet"] or bool(info.get("duet_disabled")),
            "disable_stitch": not opts["allow_stitch"] or bool(info.get("stitch_disabled")),
            "brand_content_toggle": bool(opts.get("branded_content", False)),
            "brand_organic_toggle": bool(opts.get("your_brand", False)),
            "is_aigc": bool(req.synthetic_media),
        }
        init = check(self.http.post(f"{API}/v2/post/publish/video/init/", headers=self._h(), timeout=60, json={
            "post_info": post_info,
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
                            "total_chunk_count": count}}), "post init").get("data", {})
        publish_id, upload_url = init.get("publish_id"), init.get("upload_url")
        if not (publish_id and upload_url):
            raise PlatformError("TikTok init returned no publish_id / upload_url")
        with open(req.file_path, "rb") as f:
            for i in range(count):
                first = i * chunk_size
                data = f.read(size - first if i == count - 1 else chunk_size)
                check(self.http.put(upload_url, data=data, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {first}-{first + len(data) - 1}/{size}"}), f"upload chunk {i + 1}")
        return PublishResult(publish_id, None, privacy, init)

    def status(self, publish_id: str) -> dict:
        return check(self.http.post(f"{API}/v2/post/publish/status/fetch/", headers=self._h(), timeout=30,
                                    json={"publish_id": publish_id}), "post status").get("data", {})

    def metrics(self, external_id: str) -> dict:
        # external_id must be the public video id (from the status call), which needs the video.list scope.
        data = check(self.http.post(f"{API}/v2/video/query/", headers=self._h(), timeout=60,
                                    params={"fields": "id,view_count,like_count,comment_count,share_count"},
                                    json={"filters": {"video_ids": [external_id]}}), "video query")
        v = (data.get("data", {}).get("videos") or [{}])[0]
        return {"views": v.get("view_count"), "likes": v.get("like_count"), "comments": v.get("comment_count"),
                "shares": v.get("share_count"), "watch_time_s": None, "avg_view_pct": None,
                "followers_delta": None, "raw": data}

    def take_down(self, external_id: str) -> bool:
        # The Content Posting API has no delete endpoint. A person must delete the post in the TikTok app.
        return False
