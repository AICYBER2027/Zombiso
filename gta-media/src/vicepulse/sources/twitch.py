"""Twitch Helix API, read-only with an app access token (client-credentials flow; free).

Endpoints used: games, streams, users, channels, channels/followers (total only), schedule, videos.
"""

from __future__ import annotations

import time

import requests

from . import SourceError

AUTH = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix"


class Twitch:
    def __init__(self, client_id: str, client_secret: str, *, session: requests.Session | None = None):
        if not (client_id and client_secret):
            raise SourceError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set")
        self.client_id, self.client_secret = client_id, client_secret
        self.http = session or requests.Session()
        self._token: str | None = None
        self._expires = 0.0
        self.requests = 0

    def _auth(self) -> dict:
        if self._token is None or time.time() > self._expires - 60:
            r = self.http.post(AUTH, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                           "grant_type": "client_credentials"}, timeout=30)
            if r.status_code >= 400:
                raise SourceError(f"Twitch token failed: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            self._token, self._expires = body["access_token"], time.time() + float(body.get("expires_in", 3600))
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params, *, allow_404: bool = False) -> dict:
        self.requests += 1
        r = self.http.get(f"{API}/{path}", params=params, headers=self._auth(), timeout=30)
        if allow_404 and r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise SourceError(f"Twitch {path} failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def game_id(self, name: str) -> str | None:
        data = self._get("games", {"name": name}).get("data") or []
        return data[0]["id"] if data else None

    def streams_for_game(self, game_id: str, *, language: str | None = None, first: int = 100) -> list[dict]:
        params = [("game_id", game_id), ("first", first)]
        if language:
            params.append(("language", language))
        return self._get("streams", params).get("data") or []

    def streams_for_users(self, user_ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(user_ids), 100):
            out += self._get("streams", [("user_id", u) for u in user_ids[i:i + 100]] + [("first", 100)]).get("data") or []
        return out

    def users(self, *, logins: list[str] | None = None, ids: list[str] | None = None) -> list[dict]:
        params = [("login", x) for x in (logins or [])] + [("id", x) for x in (ids or [])]
        return self._get("users", params).get("data") or [] if params else []

    def channels(self, ids: list[str]) -> list[dict]:
        return self._get("channels", [("broadcaster_id", x) for x in ids]).get("data") or [] if ids else []

    def followers_total(self, broadcaster_id: str) -> int | None:
        """Follower count, or None when Twitch will not give it to this kind of token."""
        try:
            return self._get("channels/followers", {"broadcaster_id": broadcaster_id, "first": 1}).get("total")
        except SourceError as e:
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                return None
            raise

    def schedule(self, broadcaster_id: str) -> list[dict]:
        data = self._get("schedule", {"broadcaster_id": broadcaster_id, "first": 10}, allow_404=True).get("data") or {}
        return data.get("segments") or []

    def videos(self, user_id: str, *, kind: str = "archive", first: int = 5) -> list[dict]:
        return self._get("videos", {"user_id": user_id, "type": kind, "first": first}).get("data") or []


__all__ = ["Twitch", "SourceError"]
