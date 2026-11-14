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


def import_google_client_json(data_dir: Path, path: str | Path, redirect_uri: str) -> dict:
    """Save the client from the JSON file Google Cloud lets you download; returns a summary without secrets."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError(f"could not read the Google client file: {e}") from e
    kind = "web" if "web" in raw else "installed" if "installed" in raw else None
    if kind is None:
        raise ValueError("this is not a Google OAuth client file (expected a 'web' section)")
    if kind == "installed":
        raise ValueError("this client is the 'Desktop app' type; create a 'Web application' client instead, "
                         f"with the redirect URI {redirect_uri}")
    c = raw[kind]
    save_youtube_client(data_dir, c.get("client_id", ""), c.get("client_secret", ""))
    uris = c.get("redirect_uris") or []
    cid = c["client_id"]
    return {"saved": True, "client_type": kind, "client_id": cid[:6] + "..." + cid[-28:],
            "project_id": c.get("project_id"), "redirect_uri_ok": redirect_uri in uris,
            "redirect_uri_expected": redirect_uri, "redirect_uris_in_file": uris}


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
    ic, isec = instagram_client(settings.data_dir)
    out["instagram_client"] = bool(ic and isec)
    out["instagram"] = {}
    for ch in settings.channels():
        key = ch["key"]
        ig = load(settings.data_dir, f"instagram_{key}")
        ig_env = bool(os.environ.get(f"INSTAGRAM_ACCESS_TOKEN_{key.upper()}"))
        out["instagram"][key] = {"name": ch.get("name", key), "connected": ig_env or bool(ig.get("access_token")),
                                 "username": ig.get("username") or ("set in .env" if ig_env else None),
                                 "days_left": max(0, (ig.get("expires_at", 0) - int(time.time())) // 86400) if ig else None}
        env = bool(os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{key.upper()}"))
        saved = load(settings.data_dir, f"youtube_{key}")
        out["youtube"][key] = {"name": ch.get("name", key), "connected": env or bool(saved.get("refresh_token")),
                               "channel_title": saved.get("channel_title") or ("set in .env" if env else None),
                               "channel_id": saved.get("channel_id")}
    return out


# ---------------------------------------------------------------- Instagram (Instagram API with Instagram Login)

IG_AUTH_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"
INSTAGRAM_SCOPES = ["instagram_business_basic", "instagram_business_content_publish",
                    "instagram_business_manage_insights"]
IG_REFRESH_WITHIN_S = 15 * 86400        # refresh long-lived tokens when less than 15 days are left


def instagram_client(data_dir: Path) -> tuple[str, str]:
    cid, sec = os.environ.get("INSTAGRAM_APP_ID", ""), os.environ.get("INSTAGRAM_APP_SECRET", "")
    if cid and sec:
        return cid, sec
    saved = load(data_dir, "instagram_client")
    return saved.get("app_id", ""), saved.get("app_secret", "")


def save_instagram_client(data_dir: Path, app_id: str, app_secret: str) -> None:
    app_id, app_secret = app_id.strip(), app_secret.strip()
    if not app_id.isdigit():
        raise ValueError("the Instagram app ID is a number (Instagram > API setup with Instagram login > "
                         "Business login settings)")
    if not (len(app_secret) >= 16 and app_secret.isalnum()):
        raise ValueError("the Instagram app secret looks wrong (32 letters and digits, shown after 'Show')")
    save(data_dir, "instagram_client", {"app_id": app_id, "app_secret": app_secret})


def instagram_auth_url(data_dir: Path, redirect_uri: str, state: str) -> str:
    cid, _ = instagram_client(data_dir)
    if not cid:
        raise ValueError("add the Instagram app ID and secret first")
    # force_reauth lets you pick a different Instagram account for the second channel.
    return IG_AUTH_URL + "?" + urlencode({"client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
                                          "scope": ",".join(INSTAGRAM_SCOPES), "state": state,
                                          "force_reauth": "true", "enable_fb_login": "0"})


def _json(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200:
        msg = (data.get("error_message") or (data.get("error") or {}).get("message") if isinstance(data, dict) else None)
        raise ValueError(f"Instagram refused the {what} ({r.status_code}): {msg or r.text[:200]}")
    return data


def instagram_connect(data_dir: Path, channel_key: str, code: str, redirect_uri: str,
                      session: requests.Session | None = None) -> dict:
    """Code -> short-lived token -> 60-day token; check the account type; save. Nothing is posted."""
    http = session or requests.Session()
    cid, sec = instagram_client(data_dir)
    code = code.strip().removesuffix("#_")
    short = _json(http.post(IG_TOKEN_URL, data={"client_id": cid, "client_secret": sec, "grant_type": "authorization_code",
                                                "redirect_uri": redirect_uri, "code": code}, timeout=30), "sign-in code")
    if isinstance(short.get("data"), list) and short["data"]:
        short = short["data"][0]
    granted = short.get("permissions") or []
    if isinstance(granted, str):
        granted = granted.split(",")
    if granted and "instagram_business_content_publish" not in granted:
        raise ValueError("publishing permission was not granted; connect again and allow all requested permissions")
    long = _json(http.get(f"{IG_GRAPH}/access_token", params={"grant_type": "ig_exchange_token", "client_secret": sec,
                                                               "access_token": short["access_token"]}, timeout=30),
                 "long-lived token")
    me = _json(http.get(f"{IG_GRAPH}/me", params={"fields": "user_id,username,account_type",
                                                  "access_token": long["access_token"]}, timeout=30), "profile lookup")
    if me.get("account_type") not in (None, "BUSINESS", "MEDIA_CREATOR"):
        raise ValueError(f"@{me.get('username')} is a personal account; switch it to a professional (Business or "
                         "Creator) account in the Instagram app, then connect again")
    now = int(time.time())
    info = {"access_token": long["access_token"], "user_id": str(me.get("user_id") or short.get("user_id")),
            "username": me.get("username"), "account_type": me.get("account_type"),
            "expires_at": now + int(long.get("expires_in") or 60 * 86400), "connected_at": now}
    save(data_dir, f"instagram_{channel_key}", info)
    return {"username": info["username"], "user_id": info["user_id"]}


def instagram_credentials(data_dir: Path, channel_key: str, session: requests.Session | None = None) -> tuple[str, str]:
    """(user_id, access_token) for the connected account, refreshing the 60-day token when it is getting old."""
    info = load(data_dir, f"instagram_{channel_key}")
    if not info.get("access_token"):
        return "", ""
    now = int(time.time())
    if info.get("expires_at", 0) - now < IG_REFRESH_WITHIN_S and now - info.get("connected_at", now) > 86400:
        http = session or requests.Session()
        try:
            new = _json(http.get(f"{IG_GRAPH}/refresh_access_token", params={
                "grant_type": "ig_refresh_token", "access_token": info["access_token"]}, timeout=30), "token refresh")
            info.update(access_token=new["access_token"], expires_at=now + int(new.get("expires_in") or 60 * 86400),
                        connected_at=now)
            save(data_dir, f"instagram_{channel_key}", info)
        except (ValueError, requests.RequestException, KeyError):
            pass                                  # the old token still works until it expires
    return info.get("user_id", ""), info["access_token"]


def code_from_pasted_url(pasted: str) -> dict:
    """The address bar after signing in, when the browser could not open the https redirect address."""
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(pasted.strip()).query)
    if not q.get("code") and not q.get("error"):
        raise ValueError("that address has no sign-in code; copy the whole address from the browser's address bar")
    return {k: v[0] for k, v in q.items()}
