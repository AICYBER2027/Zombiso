"""Connecting the owner's platform accounts from the review page (OAuth), stored in the data volume.

Secrets never appear on the page or in logs: they are written to <data>/secrets/*.json (owner-only
permissions) and read by the publishers when the matching environment variable is empty. Connecting an
account does not publish anything; publishing stays behind VICEPULSE_ALLOW_LIVE_PUBLISH.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import Settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
                  "https://www.googleapis.com/auth/youtube.readonly",
                  "https://www.googleapis.com/auth/yt-analytics.readonly"]


def secrets_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def load(data_dir: Path, name: str) -> dict:
    p = secrets_dir(data_dir) / f"{name}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data_dir: Path, name: str, value: dict) -> None:
    p = secrets_dir(data_dir) / f"{name}.json"
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(tmp, p)


# ---------------------------------------------------------------- YouTube

def youtube_client(data_dir: Path) -> tuple[str, str]:
    """OAuth client from .env if set, otherwise the one saved from the page."""
    cid, sec = os.environ.get("YOUTUBE_CLIENT_ID", ""), os.environ.get("YOUTUBE_CLIENT_SECRET", "")
    if cid and sec:
        return cid, sec
    saved = load(data_dir, "youtube_client")
    return saved.get("client_id", ""), saved.get("client_secret", "")


def save_youtube_client(data_dir: Path, client_id: str, client_secret: str) -> None:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("that does not look like a Google OAuth client ID (it ends in .apps.googleusercontent.com)")
    if len(client_secret) < 10:
        raise ValueError("the client secret looks too short")
    save(data_dir, "youtube_client", {"client_id": client_id, "client_secret": client_secret})


def youtube_auth_url(data_dir: Path, redirect_uri: str, state: str) -> str:
    cid, _ = youtube_client(data_dir)
    if not cid:
        raise ValueError("add the Google OAuth client ID and secret first")
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true", "state": state})


def youtube_connect(data_dir: Path, channel_key: str, code: str, redirect_uri: str,
                    session: requests.Session | None = None) -> dict:
    """Swap the code for tokens, look up which YouTube channel was chosen, and save the refresh token."""
    http = session or requests.Session()
    cid, sec = youtube_client(data_dir)
    r = http.post(GOOGLE_TOKEN_URL, data={"code": code, "client_id": cid, "client_secret": sec,
                                          "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200:
        raise ValueError(f"Google refused the sign-in ({r.status_code}): {r.text[:200]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise ValueError("Google did not return a long-lived token; remove VicePulse's access at "
                         "myaccount.google.com/permissions and connect again")
    ch = http.get(YT_CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30)
    items = ch.json().get("items", []) if ch.status_code == 200 else []
    if not items:
        raise ValueError("that Google account has no YouTube channel; create the channel first, then connect again")
    info = {"refresh_token": tok["refresh_token"], "channel_id": items[0]["id"],
            "channel_title": items[0]["snippet"]["title"], "connected_at": int(time.time())}
    save(data_dir, f"youtube_{channel_key}", info)
    return {"channel_id": info["channel_id"], "channel_title": info["channel_title"]}


def youtube_refresh_token(data_dir: Path, channel_key: str) -> str:
    return load(data_dir, f"youtube_{channel_key}").get("refresh_token", "")


def status(settings: Settings) -> dict:
    """What is connected, without any secret values."""
    cid, sec = youtube_client(settings.data_dir)
    out = {"youtube_client": bool(cid and sec),
           "youtube_client_from_env": bool(os.environ.get("YOUTUBE_CLIENT_ID")), "youtube": {}}
    for ch in settings.channels():
        key = ch["key"]
        env = bool(os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{key.upper()}"))
        saved = load(settings.data_dir, f"youtube_{key}")
        out["youtube"][key] = {"name": ch.get("name", key), "connected": env or bool(saved.get("refresh_token")),
                               "channel_title": saved.get("channel_title") or ("set in .env" if env else None),
                               "channel_id": saved.get("channel_id")}
    return out
