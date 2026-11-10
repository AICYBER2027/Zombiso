"""YouTube Shorts via the YouTube Data API v3 (videos.insert, resumable upload) and YouTube Analytics API."""

from __future__ import annotations

from datetime import date, timedelta

import requests

from .base import PlatformError, PublishRequest, PublishResult, check

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
GAMING_CATEGORY = "20"


class YouTubePublisher:
    platform = "youtube"

    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str, audited: bool = False,
                 session: requests.Session | None = None):
        if not (client_id and client_secret and refresh_token):
            raise PlatformError("YouTube credentials are not configured")
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.audited = audited
        self.http = session or requests.Session()
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            data = check(self.http.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"}, timeout=30), "token refresh")
            self._token = data["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def publish(self, req: PublishRequest) -> PublishResult:
        # Until the API project has passed YouTube's compliance audit, upload as private and let a
        # person publish it in YouTube Studio.
        privacy = "public" if self.audited else "private"
        tags = [h.lstrip("#") for h in req.hashtags]
        body = {
            "snippet": {"title": req.title[:100], "description": f"{req.description}\n\n{' '.join(req.hashtags)}"[:5000],
                        "tags": tags, "categoryId": GAMING_CATEGORY, "defaultLanguage": req.language,
                        "defaultAudioLanguage": req.language},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": bool(req.synthetic_media)},
        }
        size = req.file_path.stat().st_size
        start = self.http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                               headers={**self._auth(), "X-Upload-Content-Type": "video/mp4",
                                        "X-Upload-Content-Length": str(size)}, json=body, timeout=60)
        check(start, "upload start")
        location = start.headers.get("Location")
        if not location:
            raise PlatformError("YouTube did not return an upload URL")
        with open(req.file_path, "rb") as f:
            done = check(self.http.put(location, headers={**self._auth(), "Content-Type": "video/mp4",
                                                          "Content-Length": str(size)}, data=f, timeout=600), "upload")
        vid = done.get("id")
        if not vid:
            raise PlatformError("YouTube upload returned no video id")
        return PublishResult(vid, f"https://www.youtube.com/shorts/{vid}",
                             done.get("status", {}).get("privacyStatus", privacy), done)

    def metrics(self, external_id: str) -> dict:
        end = date.today()
        data = check(self.http.get(ANALYTICS_URL, headers=self._auth(), timeout=60, params={
            "ids": "channel==MINE", "startDate": str(end - timedelta(days=28)), "endDate": str(end),
            "metrics": "views,estimatedMinutesWatched,averageViewPercentage,likes,comments,shares,subscribersGained",
            "filters": f"video=={external_id}"}), "analytics")
        row = (data.get("rows") or [[0] * 7])[0]
        return {"views": row[0], "watch_time_s": row[1] * 60, "avg_view_pct": row[2], "likes": row[3],
                "comments": row[4], "shares": row[5], "followers_delta": row[6], "raw": data}

    def take_down(self, external_id: str) -> bool:
        check(self.http.delete(VIDEOS_URL, params={"id": external_id}, headers=self._auth(), timeout=60), "delete")
        return True
