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
    tc, tsec = tiktok_client(settings.data_dir)
    out["tiktok_client"] = bool(tc and tsec)
    out["instagram"] = {}
    for ch in settings.channels():
        key = ch["key"]
        ig = load(settings.data_dir, f"instagram_{key}")
        ig_env = bool(os.environ.get(f"INSTAGRAM_ACCESS_TOKEN_{key.upper()}"))
        out["instagram"][key] = {"name": ch.get("name", key), "connected": ig_env or bool(ig.get("access_token")),
                                 "username": ig.get("username") or ("set in .env" if ig_env else None),
                                 "expected": ch.get("instagram_username"), "token_kind": ig.get("token_kind"),
                                 "user_id": ig.get("user_id"),
                                 "days_left": (max(0, (ig["expires_at"] - int(time.time())) // 86400)
                                               if ig.get("expires_at") else None)}
        tt = load(settings.data_dir, f"tiktok_{key}")
        out.setdefault("tiktok", {})[key] = {
            "name": ch.get("name", key), "connected": bool(tt.get("access_token")), "username": tt.get("username"),
            "nickname": tt.get("nickname"), "expected": ch.get("tiktok_username"),
            "renew_days_left": (max(0, (tt["refresh_expires_at"] - int(time.time())) // 86400)
                                if tt.get("refresh_expires_at") else None)}
        fb = load(settings.data_dir, f"facebook_{key}")
        fb_env = bool(os.environ.get(f"FACEBOOK_ACCESS_TOKEN_{key.upper()}"))
        out.setdefault("facebook", {})[key] = {
            "name": ch.get("name", key), "connected": fb_env or bool(fb.get("access_token")),
            "page_name": fb.get("page_name") or ("set in .env" if fb_env else None), "page_id": fb.get("page_id"),
            "expected": ch.get("facebook_page"), "can_post": fb.get("can_post")}
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
    try:   # this Meta call needs the token as a parameter; never let the URL reach an error message
        long = _json(http.get(f"{IG_GRAPH}/access_token", params={"grant_type": "ig_exchange_token", "client_secret": sec,
                                                                   "access_token": short["access_token"]}, timeout=30),
                     "long-lived token")
        me = _json(http.get(f"{IG_GRAPH}/me", params={"fields": "user_id,username,account_type"},
                            headers={"Authorization": f"Bearer {long['access_token']}"}, timeout=30), "profile lookup")
    except requests.RequestException as ex:
        raise ValueError(f"could not reach Instagram ({type(ex).__name__}); try again") from None
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
    if info.get("expires_at") and info.get("token_kind", "instagram_login") == "instagram_login" and info.get("expires_at", 0) - now < IG_REFRESH_WITHIN_S and now - info.get("connected_at", now) > 86400:
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


FB_GRAPH = "https://graph.facebook.com"


def instagram_graph(data_dir: Path, channel_key: str) -> str:
    return load(data_dir, f"instagram_{channel_key}").get("graph") or IG_GRAPH


def meta_app(data_dir: Path) -> tuple[str, str]:
    """The Meta (Facebook) app ID and secret, used only to ask Meta which accounts a token was granted."""
    cid, sec = os.environ.get("META_APP_ID", ""), os.environ.get("META_APP_SECRET", "")
    if cid and sec:
        return cid, sec
    saved = load(data_dir, "meta_app")
    return saved.get("app_id", ""), saved.get("app_secret", "")


def save_meta_app(data_dir: Path, app_id: str, app_secret: str) -> None:
    app_id, app_secret = app_id.strip(), app_secret.strip()
    if not app_id.isdigit():
        raise ValueError("the Meta app ID is a number (App settings > Basic > App ID)")
    if not (len(app_secret) >= 16 and app_secret.isalnum()):
        raise ValueError("the Meta app secret looks wrong (App settings > Basic > App secret, after 'Show')")
    save(data_dir, "meta_app", {"app_id": app_id, "app_secret": app_secret})


def _token_kind_label(token: str) -> str:
    return "EAA (Facebook/system-user)" if token.startswith("EAA") else "IG (Instagram)" if token.startswith("IG") else "other"


def _meta_error(r: requests.Response | None) -> str:
    if r is None:
        return "no response"
    try:
        err = r.json().get("error", {})
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
    except ValueError:
        msg = ""
    return f"{r.status_code} {msg}".strip()[:180]


def find_instagram_account(token: str, *, ig_user_id: str = "", expected_username: str | None = None,
                           session: requests.Session | None = None,
                           app: tuple[str, str] | None = None) -> tuple[dict | None, list[str]]:
    """Which Instagram professional account a Meta token opens, trying every supported route.

    The token goes only in the Authorization header (never in a URL, so it cannot leak into error text).
    Returns (account or None, list of attempts with Meta's error messages, no secrets).
    """
    http = session or requests.Session()
    hdr = {"Authorization": f"Bearer {token}"}
    tried: list[str] = []

    def get(url, params, label):
        try:
            r = http.get(url, params=params, headers=hdr, timeout=30)
        except requests.RequestException as ex:
            tried.append(f"{label}: network error ({type(ex).__name__})")
            return None
        if r.status_code != 200:
            tried.append(f"{label}: {_meta_error(r)}")
            return None
        try:
            return r.json()
        except ValueError:
            tried.append(f"{label}: not JSON")
            return None

    def ok(acc):
        return not expected_username or (acc.get("username") or "").lower() == expected_username.lower()

    candidates: list[dict] = []
    # 1) Instagram Login / Unified Login tokens identify themselves on graph.instagram.com
    me = get(f"{IG_GRAPH}/me", {"fields": "user_id,username,account_type"}, "graph.instagram.com/me")
    if me and me.get("username"):
        candidates.append({"graph": IG_GRAPH, "user_id": str(me.get("user_id") or me.get("id")),
                           "username": me["username"], "token_kind": "instagram_login"})
    # 2) the token's own grants list the Instagram account IDs it may use. A system-user token cannot inspect
    #    itself, so ask with the app's own credentials when they are saved (sent as a header, never in a URL).
    if not any(ok(c) for c in candidates):
        if app and app[0] and app[1]:
            try:
                r = http.get(f"{FB_GRAPH}/debug_token", params={"input_token": token},
                             headers={"Authorization": f"Bearer {app[0]}|{app[1]}"}, timeout=30)
                dbg = r.json() if r.status_code == 200 else None
                if dbg is None:
                    tried.append(f"graph.facebook.com/debug_token (app): {_meta_error(r)}")
            except (requests.RequestException, ValueError) as ex:
                tried.append(f"graph.facebook.com/debug_token (app): {type(ex).__name__}")
                dbg = None
        else:
            dbg = get(f"{FB_GRAPH}/debug_token", {"input_token": token}, "graph.facebook.com/debug_token")
        ids: list[str] = []
        data = (dbg or {}).get("data") or {}
        for g in data.get("granular_scopes") or []:
            if g.get("scope", "").startswith("instagram"):
                ids += [str(t) for t in g.get("target_ids") or []]
        kind = "system_user" if data.get("type") == "SYSTEM_USER" else "facebook_user"
        if dbg is not None and app and app[0] and app[1]:
            # report what Meta says this token holds (names and counts only, no secrets)
            scopes = sorted(str(s) for s in data.get("scopes") or [])
            tried.append(f"graph.facebook.com/debug_token (app) ok: type {data.get('type') or '?'}, "
                         f"valid {bool(data.get('is_valid'))}, app {data.get('app_id') or '?'}, "
                         f"permissions [{', '.join(scopes) or 'none'}], Instagram accounts granted {len(ids)}")
        for i in dict.fromkeys(([ig_user_id] if ig_user_id else []) + ids):
            acc = get(f"{FB_GRAPH}/{i}", {"fields": "id,username"}, f"graph.facebook.com/<account {i}>")
            if acc and acc.get("username"):
                candidates.append({"graph": FB_GRAPH, "user_id": str(acc["id"]), "username": acc["username"],
                                   "token_kind": kind})
    # 3) the given ID on graph.instagram.com (Instagram-scoped IDs)
    if ig_user_id and not any(ok(c) for c in candidates):
        acc = get(f"{IG_GRAPH}/{ig_user_id}", {"fields": "user_id,username"}, f"graph.instagram.com/<account {ig_user_id}>")
        if acc and acc.get("username"):
            candidates.append({"graph": IG_GRAPH, "user_id": str(acc.get("user_id") or ig_user_id),
                               "username": acc["username"], "token_kind": "instagram_login"})
    # 4) Facebook Page-linked accounts
    if not any(ok(c) for c in candidates):
        pages = get(f"{FB_GRAPH}/me/accounts", {"fields": "instagram_business_account{id,username}", "limit": "100"},
                    "graph.facebook.com/me/accounts")
        found_pages = 0
        for p in (pages or {}).get("data", []):
            a = p.get("instagram_business_account")
            if a and a.get("username"):
                found_pages += 1
                candidates.append({"graph": FB_GRAPH, "user_id": str(a["id"]), "username": a["username"],
                                   "token_kind": "facebook_user"})
        if pages is not None and not found_pages:
            tried.append("graph.facebook.com/me/accounts: no Facebook Page with an Instagram account")
    # 5) accounts assigned to a system user directly, through the business portfolio
    if not any(ok(c) for c in candidates):
        biz = get(f"{FB_GRAPH}/me/businesses", {"fields": "id,name", "limit": "25"}, "graph.facebook.com/me/businesses")
        for b in (biz or {}).get("data", []):
            for edge in ("instagram_business_accounts", "owned_instagram_accounts"):
                lst = get(f"{FB_GRAPH}/{b['id']}/{edge}", {"fields": "id,username", "limit": "100"},
                          f"graph.facebook.com/<business>/{edge}")
                for a in (lst or {}).get("data", []):
                    if not a.get("username"):
                        continue
                    # confirm it is an Instagram professional account this token can use
                    acc = get(f"{FB_GRAPH}/{a['id']}", {"fields": "id,username"}, f"graph.facebook.com/<account {a['id']}>")
                    if acc and acc.get("username"):
                        candidates.append({"graph": FB_GRAPH, "user_id": str(acc["id"]), "username": acc["username"],
                                           "token_kind": "system_user"})
                if any(ok(c) for c in candidates):
                    break
        if biz is not None and not (biz or {}).get("data"):
            tried.append("graph.facebook.com/me/businesses: none visible to this token")
    match = [c for c in candidates if ok(c)]
    if match:
        return match[0], tried
    if candidates:
        tried.append("found only: " + ", ".join(sorted({"@" + c["username"] for c in candidates})))
    return None, tried


def instagram_save_token(data_dir: Path, channel_key: str, token: str, *, ig_user_id: str = "",
                         expected_username: str | None = None, session: requests.Session | None = None) -> dict:
    """Check a token Meta generated (e.g. the Unified Login 'Generate Token' button) and store it for one channel.

    Verifies which Instagram account it opens, that it is the account this channel expects, and that it may
    publish (reads the content publishing limit, which needs the publish permission). Nothing is posted.
    """
    http = session or requests.Session()
    token, ig_user_id = token.strip(), ig_user_id.strip()
    if len(token) < 40 or any(c.isspace() for c in token):
        raise ValueError("that does not look like a Meta access token (a long string, usually starting EAA or IG)")
    if ig_user_id and not ig_user_id.isdigit():
        raise ValueError("the Instagram account ID is a number; leave it empty if you are not sure")
    found, tried = find_instagram_account(token, ig_user_id=ig_user_id, expected_username=expected_username,
                                          session=http, app=meta_app(data_dir))
    if found is None:
        others = [t for t in tried if t.startswith("found only")]
        if others:
            raise ValueError(f"this token does not open @{expected_username} ({others[0][len('found only: '):]}); "
                             "generate the token next to the right account")
        app_id, app_sec = meta_app(data_dir)
        sys_ok = any("type SYSTEM_USER, valid True" in t and "instagram_content_publish" in t for t in tried)
        hint = (" Next: this system-user token is valid and may publish, but Meta does not list its Instagram "
                "accounts (system-user grants usually carry no account list). Use the per-channel row instead and "
                "type the Instagram account ID (Business settings > Accounts > Instagram accounts > click the account; "
                "the ID is shown there)." if sys_ok and not ig_user_id else
                " Next: in Meta, generate the token again with instagram_basic and instagram_content_publish "
                "(or instagram_business_basic and instagram_business_content_publish) ticked, and make sure the "
                "Instagram account is assigned to the system user (Business settings > Users > System users > "
                "Assign assets)." if app_id and app_sec else
                " Next: save the Meta app ID and app secret (App settings > Basic) on this page, then try again; "
                "Meta will then list the exact Instagram accounts this token was granted.")
        raise ValueError(f"Meta did not identify an Instagram account for this {_token_kind_label(token)} token. "
                         "Tried: " + " | ".join(tried) + "." + hint)
    try:
        r = http.get(f"{found['graph']}/{found['user_id']}/content_publishing_limit",
                     params={"fields": "quota_usage,config"}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except requests.RequestException as ex:
        raise ValueError(f"could not reach Meta to check publishing permission ({type(ex).__name__})") from None
    if r.status_code != 200:
        raise ValueError(f"@{found['username']} was found (ID {found['user_id']}), but the token is not allowed to "
                         f"publish Reels ({_meta_error(r)}); generate it with the content publishing permission")
    now = int(time.time())
    # Only Instagram Login tokens are renewed by VicePulse (60 days); system-user tokens do not expire.
    renewable = found["graph"] == IG_GRAPH and found["token_kind"] == "instagram_login" and token.startswith("IG")
    save(data_dir, f"instagram_{channel_key}", {**found, "access_token": token, "connected_at": now,
                                                 "expires_at": now + 60 * 86400 if renewable else None})
    return {"username": found["username"], "user_id": found["user_id"], "token_kind": found["token_kind"],
            "graph": found["graph"], "publish_permission": True}


# ---------------------------------------------------------------- TikTok (Login Kit for Web + Content Posting API)

TT_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TT_API = "https://open.tiktokapis.com"
TT_TOKEN_URL = f"{TT_API}/v2/oauth/token/"
TIKTOK_SCOPES = ["user.info.basic", "video.publish"]
TT_REFRESH_WITHIN_S = 10 * 60          # access tokens are short-lived; renew when <10 minutes remain


def tiktok_client(data_dir: Path) -> tuple[str, str]:
    cid, sec = os.environ.get("TIKTOK_CLIENT_KEY", ""), os.environ.get("TIKTOK_CLIENT_SECRET", "")
    if cid and sec:
        return cid, sec
    saved = load(data_dir, "tiktok_client")
    return saved.get("client_key", ""), saved.get("client_secret", "")


def save_tiktok_client(data_dir: Path, client_key: str, client_secret: str) -> None:
    client_key, client_secret = client_key.strip(), client_secret.strip()
    if not (8 <= len(client_key) <= 64 and client_key.isalnum()):
        raise ValueError("the TikTok client key looks wrong (letters and digits, shown under App details > Credentials)")
    if not (16 <= len(client_secret) <= 128) or any(c.isspace() for c in client_secret):
        raise ValueError("the TikTok client secret looks wrong (shown under App details > Credentials)")
    save(data_dir, "tiktok_client", {"client_key": client_key, "client_secret": client_secret})


def tiktok_auth_url(data_dir: Path, redirect_uri: str, state: str) -> str:
    cid, _ = tiktok_client(data_dir)
    if not cid:
        raise ValueError("add the TikTok client key and secret first")
    # disable_auto_auth=1 always shows TikTok's consent screen, so the right account can be picked per channel.
    return TT_AUTH_URL + "?" + urlencode({"client_key": cid, "response_type": "code", "scope": ",".join(TIKTOK_SCOPES),
                                          "redirect_uri": redirect_uri, "state": state, "disable_auto_auth": "1"})


def _tt_json(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    # OAuth endpoints answer {"error": "invalid_grant", "error_description": ...}; the API answers
    # {"error": {"code": "ok" | "...", "message": ...}}.
    if isinstance(err, dict):
        bad = err.get("code") not in (None, "", "ok")
        msg = err.get("message") or err.get("code")
    else:
        bad, msg = bool(err), data.get("error_description") or err
    if r.status_code != 200 or bad:
        raise ValueError(f"TikTok refused the {what} ({r.status_code}): {msg or r.text[:200]}")
    return data


def tiktok_creator_info(token: str, session: requests.Session | None = None) -> dict:
    """The posting account as TikTok's Content Posting API sees it (needs video.publish). Read-only."""
    http = session or requests.Session()
    try:
        r = http.post(f"{TT_API}/v2/post/publish/creator_info/query/", timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    except requests.RequestException as ex:
        raise ValueError(f"could not reach TikTok ({type(ex).__name__}); try again") from None
    return _tt_json(r, "creator info").get("data") or {}


def tiktok_connect(data_dir: Path, channel_key: str, code: str, redirect_uri: str, *, expected_username: str | None = None,
                   session: requests.Session | None = None) -> dict:
    """Code -> access + refresh token; confirm video.publish and which account it is; save. Nothing is posted."""
    http = session or requests.Session()
    cid, sec = tiktok_client(data_dir)
    if not (cid and sec):
        raise ValueError("add the TikTok client key and secret first")
    try:
        tok = _tt_json(http.post(TT_TOKEN_URL, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"},
                                 data={"client_key": cid, "client_secret": sec, "code": code.strip(),
                                       "grant_type": "authorization_code", "redirect_uri": redirect_uri}), "sign-in code")
    except requests.RequestException as ex:
        raise ValueError(f"could not reach TikTok ({type(ex).__name__}); try again") from None
    granted = [s.strip() for s in str(tok.get("scope") or "").split(",") if s.strip()]
    if "video.publish" not in granted:
        raise ValueError("TikTok did not grant video.publish; connect again and allow posting "
                         "(and check that Content Posting API is added to the app)")
    info = tiktok_creator_info(tok["access_token"], session=http)
    username = info.get("creator_username") or ""
    if expected_username and username.lower() != expected_username.lower().lstrip("@"):
        raise ValueError(f"you signed in as @{username}, but {channel_key.upper()} expects @{expected_username}; "
                         "log out of TikTok in the browser (or use a private window) and connect again")
    now = int(time.time())
    save(data_dir, f"tiktok_{channel_key}", {
        "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
        "expires_at": now + int(tok.get("expires_in") or 0),
        "refresh_expires_at": now + int(tok.get("refresh_expires_in") or 0),
        "open_id": tok.get("open_id"), "scope": granted, "username": username,
        "nickname": info.get("creator_nickname"), "connected_at": now})
    return {"username": username, "nickname": info.get("creator_nickname"), "scope": granted}


def tiktok_credentials(data_dir: Path, channel_key: str, session: requests.Session | None = None) -> str:
    """A valid access token for the channel's TikTok account, renewing it when needed ('' if not connected).

    TikTok rotates refresh tokens: every refresh returns a new one and invalidates the old, so it is saved at once.
    """
    info = load(data_dir, f"tiktok_{channel_key}")
    if not info.get("access_token"):
        return ""
    now = int(time.time())
    if info.get("expires_at", 0) - now > TT_REFRESH_WITHIN_S:
        return info["access_token"]
    cid, sec = tiktok_client(data_dir)
    if not (info.get("refresh_token") and cid and sec):
        raise ValueError(f"the TikTok login for {channel_key.upper()} expired; press Reconnect on the Accounts page")
    http = session or requests.Session()
    try:
        new = _tt_json(http.post(TT_TOKEN_URL, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"},
                                 data={"client_key": cid, "client_secret": sec, "grant_type": "refresh_token",
                                       "refresh_token": info["refresh_token"]}), "login renewal")
    except requests.RequestException as ex:
        raise ValueError(f"could not reach TikTok to renew the login ({type(ex).__name__}); try again") from None
    except ValueError:
        raise ValueError(f"the TikTok login for {channel_key.upper()} could not be renewed; press Reconnect on the "
                         "Accounts page") from None
    info.update(access_token=new["access_token"], refresh_token=new.get("refresh_token") or info["refresh_token"],
                expires_at=now + int(new.get("expires_in") or 0))
    if new.get("refresh_expires_in"):
        info["refresh_expires_at"] = now + int(new["refresh_expires_in"])
    save(data_dir, f"tiktok_{channel_key}", info)
    return info["access_token"]


# ---------------------------------------------------------------- Twitch (VicePulse's own account, for live clips)

TW_AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TW_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TW_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
# clips:edit = make a clip of a live stream; editor:manage:clips = download clips of channels where we are editor.
TWITCH_SCOPES = ["clips:edit", "editor:manage:clips"]


def twitch_app() -> tuple[str, str]:
    return os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")


def twitch_auth_url(redirect_uri: str, state: str) -> str:
    cid, _ = twitch_app()
    if not cid:
        raise ValueError("TWITCH_CLIENT_ID is not set in .env")
    return TW_AUTH_URL + "?" + urlencode({"client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
                                          "scope": " ".join(TWITCH_SCOPES), "state": state, "force_verify": "true"})


def _tw_token(http, data: dict, what: str) -> dict:
    try:
        r = http.post(TW_TOKEN_URL, data=data, timeout=30)
    except requests.RequestException as ex:
        raise ValueError(f"could not reach Twitch ({type(ex).__name__}); try again") from None
    if r.status_code != 200:
        try:
            msg = r.json().get("message")
        except ValueError:
            msg = r.text[:200]
        raise ValueError(f"Twitch refused the {what} ({r.status_code}): {msg}")
    return r.json()


def twitch_connect(data_dir: Path, code: str, redirect_uri: str, session: requests.Session | None = None) -> dict:
    http = session or requests.Session()
    cid, sec = twitch_app()
    tok = _tw_token(http, {"client_id": cid, "client_secret": sec, "code": code.strip(),
                           "grant_type": "authorization_code", "redirect_uri": redirect_uri}, "sign-in code")
    scopes = tok.get("scope") or []
    missing = [s for s in TWITCH_SCOPES if s not in scopes]
    if missing:
        raise ValueError(f"Twitch did not grant {', '.join(missing)}; connect again and allow everything")
    v = http.get(TW_VALIDATE_URL, headers={"Authorization": f"OAuth {tok['access_token']}"}, timeout=30)
    who = v.json() if v.status_code == 200 else {}
    now = int(time.time())
    save(data_dir, "twitch_user", {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
                                   "expires_at": now + int(tok.get("expires_in") or 0), "scope": scopes,
                                   "user_id": who.get("user_id"), "login": who.get("login"), "connected_at": now})
    return {"login": who.get("login"), "user_id": who.get("user_id"), "scope": scopes}


def twitch_user_token(data_dir: Path, session: requests.Session | None = None) -> tuple[str, str]:
    """(user_id, access token) of VicePulse's Twitch account, renewed when it is close to expiring."""
    info = load(data_dir, "twitch_user")
    if not info.get("access_token"):
        raise ValueError("VicePulse's Twitch account is not connected (Accounts page > Twitch)")
    if info.get("expires_at", 0) - time.time() < 300:
        cid, sec = twitch_app()
        new = _tw_token(session or requests.Session(), {"client_id": cid, "client_secret": sec,
                                                        "grant_type": "refresh_token",
                                                        "refresh_token": info.get("refresh_token", "")}, "login renewal")
        info.update(access_token=new["access_token"], refresh_token=new.get("refresh_token") or info["refresh_token"],
                    expires_at=int(time.time()) + int(new.get("expires_in") or 0))
        save(data_dir, "twitch_user", info)
    return info.get("user_id") or "", info["access_token"]


def verify(settings: Settings, session: requests.Session | None = None) -> dict:
    """Ask Google and Meta which account each stored login opens, and compare with the channel config.

    Read-only (no uploads). Returns names, IDs and yes/no results only, never tokens.
    """
    http = session or requests.Session()
    out: dict = {"youtube": {}, "instagram": {}, "live_publishing": "on" if settings.allow_live_publish else "off"}
    cid, sec = youtube_client(settings.data_dir)
    for ch in settings.channels():
        key = ch["key"]
        saved = load(settings.data_dir, f"youtube_{key}")
        rt = os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{key.upper()}") or saved.get("refresh_token")
        if not rt:
            out["youtube"][key] = {"connected": False}
        else:
            try:
                tok = http.post(GOOGLE_TOKEN_URL, data={"client_id": cid, "client_secret": sec, "refresh_token": rt,
                                                        "grant_type": "refresh_token"}, timeout=30)
                if tok.status_code != 200:
                    raise ValueError(f"Google refused the stored login ({tok.status_code}); press Reconnect")
                r = http.get(YT_CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                             headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=30)
                item = (r.json().get("items") or [{}])[0] if r.status_code == 200 else {}
                handle = (item.get("snippet") or {}).get("customUrl")
                want = ch.get("youtube_handle")
                res = {"connected": True, "channel_id": item.get("id"), "title": (item.get("snippet") or {}).get("title"),
                       "handle": handle, "matches_saved_id": item.get("id") == saved.get("channel_id") if saved else None}
                if want:
                    res["matches_expected_handle"] = (handle or "").lower() == want.lower()
                out["youtube"][key] = res
            except (ValueError, KeyError, requests.RequestException) as e:
                out["youtube"][key] = {"connected": True, "ok": False,
                                       "error": str(e) if isinstance(e, ValueError) else type(e).__name__}
        user_id, token = instagram_credentials(settings.data_dir, key, session=http)
        if not token:
            out["instagram"][key] = {"connected": False, "expected": ch.get("instagram_username")}
            continue
        graph = instagram_graph(settings.data_dir, key)
        try:
            hdr = {"Authorization": f"Bearer {token}"}
            r = http.get(f"{graph}/{user_id}", params={"fields": "username"}, headers=hdr, timeout=30)
            name = r.json().get("username") if r.status_code == 200 else None
            q = http.get(f"{graph}/{user_id}/content_publishing_limit",
                         params={"fields": "quota_usage"}, headers=hdr, timeout=30)
            out["instagram"][key] = {"connected": True, "username": name, "user_id": user_id,
                                     "expected": ch.get("instagram_username"),
                                     "matches_expected": (name or "").lower() == (ch.get("instagram_username") or "").lower(),
                                     "publish_permission": q.status_code == 200,
                                     "token_kind": load(settings.data_dir, f"instagram_{key}").get("token_kind")}
        except (ValueError, requests.RequestException) as e:
            out["instagram"][key] = {"connected": True, "ok": False, "error": type(e).__name__}
    out["tiktok"] = {}
    for ch in settings.channels():
        key, want = ch["key"], ch.get("tiktok_username")
        try:
            tt_token = tiktok_credentials(settings.data_dir, key, session=http)
        except ValueError as e:
            out["tiktok"][key] = {"connected": True, "ok": False, "error": str(e)}
            continue
        if not tt_token:
            out["tiktok"][key] = {"connected": False, "expected": want}
            continue
        try:
            info = tiktok_creator_info(tt_token, session=http)
            name = info.get("creator_username")
            out["tiktok"][key] = {"connected": True, "username": name, "nickname": info.get("creator_nickname"),
                                  "expected": want,
                                  "matches_expected": (name or "").lower() == want.lower().lstrip("@") if want else None,
                                  "publish_permission": True,      # creator_info answers only with video.publish
                                  "privacy_options": info.get("privacy_level_options"),
                                  "account_private": "PUBLIC_TO_EVERYONE" not in (info.get("privacy_level_options") or []),
                                  "max_video_seconds": info.get("max_video_post_duration_sec")}
        except ValueError as e:
            out["tiktok"][key] = {"connected": True, "ok": False, "error": str(e)}
    return out


# ---------------------------------------------------------------- Facebook Pages (Reels)
# Same Meta app as Instagram. A system-user (or user) token is pasted once; we ask Meta which Pages it can manage,
# pick the channel's Page and keep that Page's own token. Nothing is posted here.
FB_PAGE_TASKS_NEEDED = {"CREATE_CONTENT"}


def facebook_save_page(data_dir: Path, channel_key: str, token: str, *, page_id: str = "",
                       expected_name: str | None = None, session: requests.Session | None = None) -> dict:
    """Find the channel's Facebook Page for this token and store its Page token. Returns what was saved (no token)."""
    token = (token or "").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("paste the whole token (one line, no spaces)")
    http = session or requests.Session()
    from .platforms.facebook import VERSION

    r = http.get(f"{FB_GRAPH}/{VERSION}/me/accounts", timeout=30,
                 params={"fields": "id,name,access_token,tasks", "limit": 100, "access_token": token})
    if r.status_code >= 400:
        raise ValueError(f"Meta refused the token: {_meta_error(r)}")
    pages = (r.json() or {}).get("data") or []
    if not pages:
        raise ValueError("this token cannot manage any Facebook Page. Create a Page for the channel and give the "
                         "system user access to it (Business settings > Pages), then make a new token")
    pick = None
    for pg in pages:
        if page_id and str(pg.get("id")) == str(page_id):
            pick = pg
        elif not page_id and expected_name and (pg.get("name") or "").strip().lower() == expected_name.strip().lower():
            pick = pg
    if pick is None and not page_id and not expected_name and len(pages) == 1:
        pick = pages[0]
    if pick is None:
        names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in pages)
        raise ValueError(f"the channel's Page was not found among the token's Pages: {names}")
    if not pick.get("access_token"):
        raise ValueError("Meta did not return a Page token; the token needs pages_show_list and pages_manage_posts")
    tasks = set(pick.get("tasks") or [])
    info = {"page_id": str(pick["id"]), "page_name": pick.get("name"), "access_token": pick["access_token"],
            "tasks": sorted(tasks), "can_post": FB_PAGE_TASKS_NEEDED <= tasks if tasks else None,
            "connected_at": int(time.time())}
    save(data_dir, f"facebook_{channel_key}", info)
    return {k: v for k, v in info.items() if k != "access_token"}


def facebook_credentials(data_dir: Path, channel_key: str) -> tuple[str, str]:
    info = load(data_dir, f"facebook_{channel_key}")
    return info.get("page_id", ""), info.get("access_token", "")
