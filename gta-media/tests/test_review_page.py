"""Local review page: watch, approve/reject, authorize creators; refuses cross-site and stale forms."""

from __future__ import annotations

import json
import re
import threading

import pytest
import requests

from vicepulse import cli, review


@pytest.fixture()
def made_clip(settings, conn, footage, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["highlight", "--file", str(footage), "--rights", "own", "--clip-seconds", "20"]) == 0
    return json.loads(capsys.readouterr().out)["clips"][0]


@pytest.fixture()
def server(settings):
    httpd = review.make_server(settings, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.app
    httpd.shutdown()


def token(page: str) -> str:
    return re.search(r'name=csrf value="([^"]+)"', page).group(1)


def test_review_page_shows_plays_and_decides(settings, conn, made_clip, server):
    base, _ = server
    page = requests.get(base + "/").text
    assert f"clip {made_clip}" in page and "live publishing: off" in page and "Approve" in page

    # the video streams, with seeking (HTTP range requests)
    r = requests.get(f"{base}/media/{made_clip}/youtube", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.headers["Content-Type"] == "video/mp4"
    assert requests.get(f"{base}/media/{made_clip}/nope").status_code == 404

    # stale secret and cross-site posts are refused
    assert requests.post(base + "/decide", data={"csrf": "x", "clip": made_clip, "decision": "approve"}).status_code == 403
    assert requests.post(base + "/decide", data={"csrf": token(page), "clip": made_clip, "decision": "approve"},
                         headers={"Origin": "https://evil.example"}).status_code == 403
    assert conn.execute("SELECT status FROM clips WHERE id = %s", (made_clip,)).fetchone()["status"] == "pending_approval"

    r = requests.post(base + "/decide", data={"csrf": token(page), "clip": made_clip, "decision": "approve",
                                              "reason": "looks good"})
    assert r.status_code == 200 and "approved. Nothing was published" in r.text
    conn.rollback()
    row = conn.execute("SELECT status, approved_by FROM clips WHERE id = %s", (made_clip,)).fetchone()
    assert row["status"] == "approved" and row["approved_by"] == review.REVIEWER
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0
    assert f"clip {made_clip}" in requests.get(base + "/done").text

    # deciding twice shows an error instead of crashing
    r = requests.post(base + "/decide", data={"csrf": token(page), "clip": made_clip, "decision": "reject"})
    assert "not waiting for approval" in r.text


def test_authorize_creator_from_the_page(settings, conn, server):
    base, _ = server
    cid = conn.execute("INSERT INTO creators (display_name, primary_language) VALUES ('Some Streamer', 'en') "
                       "RETURNING id").fetchone()["id"]
    conn.commit()
    page = requests.get(base + "/creators").text
    assert "Some Streamer" in page
    r = requests.post(base + "/authorize", data={"csrf": token(page), "creator": cid, "evidence": "http://not-https"})
    assert "https link" in r.text
    r = requests.post(base + "/authorize", data={"csrf": token(page), "creator": cid,
                                                 "evidence": "https://drive.example/proof.png"})
    assert "authorized" in r.text and "proof" in r.text
    conn.rollback()
    cr = conn.execute("SELECT * FROM creators WHERE id = %s", (cid,)).fetchone()
    assert cr["permission_status"] == "authorized"
    rl = conn.execute("SELECT * FROM rights_ledger WHERE id = %s", (cr["rights_id"],)).fetchone()
    assert rl["licence_type"] == "creator_permission" and rl["derivative_edits_allowed"] and rl["monetisation_allowed"]
    assert rl["document_url"] == "https://drive.example/proof.png"


class FakeGoogle:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, data))
        r = requests.Response()
        r.status_code, r._content = 200, json.dumps({"access_token": "AT", "refresh_token": "RT-secret-123"}).encode()
        return r

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer AT" and params["mine"] == "true"
        r = requests.Response()
        r.status_code, r._content = 200, json.dumps(
            {"items": [{"id": "UCvicepulseEN", "snippet": {"title": "VicePulse EN"}}]}).encode()
        return r


def test_connect_youtube_from_the_page(settings, conn, server, monkeypatch):
    import os
    from urllib.parse import parse_qs as pq, urlparse as up
    from vicepulse import accounts
    from vicepulse.platforms import make_publisher
    base, httpd_app = server
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN_EN", raising=False)
    httpd_app.http = FakeGoogle()

    page = requests.get(base + "/accounts").text
    assert "Step 1" in page and "http://localhost:8090/oauth/youtube/callback" in page
    t = token(page)
    r = requests.post(base + "/accounts/youtube-client", data={"csrf": t, "client_id": "nope", "client_secret": "x" * 20})
    assert "does not look like a Google OAuth client ID" in r.text
    r = requests.post(base + "/accounts/youtube-client",
                      data={"csrf": t, "client_id": "123-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-shh-secret"})
    assert "saved" in r.text and "GOCSPX-shh-secret" not in r.text and "Connect YouTube" in r.text

    r = requests.post(base + "/accounts/youtube-start", data={"csrf": t, "channel": "en"}, allow_redirects=False)
    assert r.status_code == 303
    loc = up(r.headers["Location"])
    q = {k: v[0] for k, v in pq(loc.query).items()}
    assert loc.netloc == "accounts.google.com" and q["access_type"] == "offline"
    assert q["redirect_uri"] == "http://localhost:8090/oauth/youtube/callback" and "youtube.upload" in q["scope"]

    assert "expired" in requests.get(base + "/oauth/youtube/callback", params={"state": "forged", "code": "c"}).text
    r = requests.get(base + "/oauth/youtube/callback", params={"state": q["state"], "code": "the-code"})
    assert "YouTube connected for EN: VicePulse EN" in r.text and "RT-secret-123" not in r.text
    assert httpd_app.http.posts[0][1]["code"] == "the-code"

    f = settings.data_dir / "secrets" / "youtube_en.json"
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"
    assert accounts.youtube_refresh_token(settings.data_dir, "en") == "RT-secret-123"
    monkeypatch.setenv("VICEPULSE_DATA_DIR", str(settings.data_dir))
    pub = make_publisher("youtube", "en")
    assert pub.refresh_token == "RT-secret-123" and pub.client_id == "123-abc.apps.googleusercontent.com"
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0


def test_import_client_json_never_prints_the_secret(settings, conn, monkeypatch, capsys, tmp_path):
    from vicepulse import accounts, cli
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    f = tmp_path / "client_secret_123.json"
    f.write_text(json.dumps({"web": {"client_id": "123456789-abcdefgh.apps.googleusercontent.com", "project_id": "vp",
                                     "client_secret": "GOCSPX-top-secret-value",
                                     "redirect_uris": ["http://localhost:8090/oauth/youtube/callback"]}}))
    assert cli.main(["youtube-client", "--json-file", str(f)]) == 0
    printed = capsys.readouterr().out
    out = json.loads(printed)
    assert out["saved"] and out["redirect_uri_ok"] and out["live_publishing"] == "off"
    assert "GOCSPX-top-secret-value" not in printed
    assert accounts.youtube_client(settings.data_dir) == ("123456789-abcdefgh.apps.googleusercontent.com",
                                                          "GOCSPX-top-secret-value")
    f.write_text(json.dumps({"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "y" * 20}}))
    assert cli.main(["youtube-client", "--json-file", str(f)]) == 1
    assert "Web application" in capsys.readouterr().err


class FakeInstagram:
    def __init__(self, account_type="BUSINESS", username="vicepulse.en"):
        self.calls, self.account_type, self.username = [], account_type, username

    def _resp(self, body, code=200):
        r = requests.Response()
        r.status_code, r._content = code, json.dumps(body).encode()
        return r

    def post(self, url, data=None, timeout=None):
        self.calls.append(("post", url, dict(data)))
        return self._resp({"data": [{"access_token": "IG-SHORT", "user_id": "1789",
                                     "permissions": "instagram_business_basic,instagram_business_content_publish"}]})

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("get", url, dict(params or {})))
        if url.endswith("/access_token"):
            assert params["grant_type"] == "ig_exchange_token" and params["access_token"] == "IG-SHORT"
            return self._resp({"access_token": "IG-LONG-secret", "token_type": "bearer", "expires_in": 5184000})
        if url.endswith("/refresh_access_token"):
            return self._resp({"access_token": "IG-REFRESHED", "expires_in": 5184000})
        assert headers["Authorization"] == "Bearer IG-LONG-secret" and "access_token" not in (params or {})
        return self._resp({"user_id": "17841400000000001", "username": self.username, "account_type": self.account_type})


def test_connect_instagram_direct_and_pasted(settings, conn, server, monkeypatch):
    from urllib.parse import parse_qs as pq, urlparse as up
    from vicepulse import accounts
    from vicepulse.platforms import make_publisher
    base, app = server
    for v in ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET", "INSTAGRAM_USER_ID_EN", "INSTAGRAM_ACCESS_TOKEN_EN"):
        monkeypatch.delenv(v, raising=False)
    app.http = FakeInstagram()
    page = requests.get(base + "/accounts").text
    assert "http://localhost:8090/oauth/instagram/callback" in page and "Instagram app ID" in page
    t = token(page)
    assert "is a number" in requests.post(base + "/accounts/instagram-client",
                                          data={"csrf": t, "app_id": "abc", "app_secret": "a" * 32}).text
    r = requests.post(base + "/accounts/instagram-client",
                      data={"csrf": t, "app_id": "1234567890", "app_secret": "0123456789abcdef0123456789abcdef"})
    assert "Instagram app saved" in r.text and "0123456789abcdef0123456789abcdef" not in r.text

    r = requests.post(base + "/accounts/instagram-start", data={"csrf": t, "channel": "en"}, allow_redirects=False)
    loc = up(r.headers["Location"])
    q = {k: v[0] for k, v in pq(loc.query).items()}
    assert loc.netloc == "www.instagram.com" and loc.path == "/oauth/authorize" and q["client_id"] == "1234567890"
    assert "instagram_business_content_publish" in q["scope"] and q["redirect_uri"].endswith("/oauth/instagram/callback")

    # direct return (code with Instagram's trailing #_ is cleaned)
    r = requests.get(base + "/oauth/instagram/callback", params={"state": q["state"], "code": "AQD-code#_"})
    assert "Instagram connected for EN: @vicepulse.en" in r.text and "IG-LONG-secret" not in r.text
    post = [c for c in app.http.calls if c[0] == "post"][0][2]
    assert post["code"] == "AQD-code" and post["redirect_uri"] == "http://localhost:8090/oauth/instagram/callback"
    assert accounts.instagram_credentials(settings.data_dir, "en") == ("17841400000000001", "IG-LONG-secret")
    monkeypatch.setenv("VICEPULSE_DATA_DIR", str(settings.data_dir))
    pub = make_publisher("instagram", "en")
    assert (pub.user_id, pub.token) == ("17841400000000001", "IG-LONG-secret")

    # pasted address (when Meta requires an https redirect the browser cannot open)
    r = requests.post(base + "/accounts/instagram-start", data={"csrf": t, "channel": "br"}, allow_redirects=False)
    st = pq(up(r.headers["Location"]).query)["state"][0]
    app.http.username = "vicepulse.br"
    r = requests.post(base + "/accounts/instagram-paste", data={
        "csrf": t, "url": f"https://localhost:8090/oauth/instagram/callback?code=XYZ&state={st}#_"})
    assert "Instagram connected for BR: @vicepulse.br" in r.text
    assert "no sign-in code" in requests.post(base + "/accounts/instagram-paste",
                                              data={"csrf": t, "url": "https://example.com/"}).text
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0


def test_instagram_personal_account_refused_and_token_refresh(settings):
    import time as _t
    from vicepulse import accounts
    accounts.save_instagram_client(settings.data_dir, "1234567890", "0123456789abcdef0123456789abcdef")
    with pytest.raises(ValueError, match="personal account"):
        accounts.instagram_connect(settings.data_dir, "en", "c", "http://localhost:8090/oauth/instagram/callback",
                                   session=FakeInstagram(account_type="PERSONAL"))
    info = {"access_token": "IG-OLD", "user_id": "1", "expires_at": int(_t.time()) + 5 * 86400,
            "connected_at": int(_t.time()) - 50 * 86400}
    accounts.save(settings.data_dir, "instagram_en", info)
    assert accounts.instagram_credentials(settings.data_dir, "en", session=FakeInstagram()) == ("1", "IG-REFRESHED")
    assert accounts.load(settings.data_dir, "instagram_en")["expires_at"] > int(_t.time()) + 50 * 86400


class FakeMetaFB:
    """graph.facebook.com for a system-user token that manages both Instagram accounts."""

    def __init__(self, can_publish=True):
        self.can_publish = can_publish
        self.accounts = {"17841400000000011": "vicepulse.en", "17841400000000022": "vicepulse.brasil"}

    def _resp(self, body, code=200):
        r = requests.Response()
        r.status_code, r._content = code, json.dumps(body).encode()
        return r

    def get(self, url, params=None, timeout=None, headers=None):
        assert "access_token" not in (params or {}), "token must never be in the URL"
        if url.startswith("https://graph.instagram.com/"):
            return self._resp({"error": {"message": "Invalid OAuth access token - Cannot parse access token"}}, 400)
        assert url.startswith("https://graph.facebook.com/")
        assert headers["Authorization"].startswith("Bearer EAA")
        path = url.split("graph.facebook.com/")[1]
        if path == "debug_token":
            return self._resp({"data": {"type": "SYSTEM_USER", "granular_scopes": []}})
        if path == "me/accounts":
            return self._resp({"data": [{"instagram_business_account": {"id": i, "username": u}}
                                        for i, u in self.accounts.items()]})
        ig, _, rest = path.partition("/")
        if ig not in self.accounts:
            return self._resp({"error": {"message": "Unsupported get request"}}, 400)
        if rest == "content_publishing_limit":
            return self._resp({"data": [{"quota_usage": 0}]} if self.can_publish else
                              {"error": {"message": "permission"}}, 200 if self.can_publish else 403)
        return self._resp({"id": ig, "username": self.accounts[ig]})


SYSTEM_TOKEN = "EAAG" + "x" * 120


def test_paste_system_user_token_maps_each_channel(settings, conn, server, monkeypatch):
    from vicepulse import accounts
    from vicepulse.platforms import make_publisher
    base, app = server
    for v in ("INSTAGRAM_USER_ID_EN", "INSTAGRAM_ACCESS_TOKEN_EN", "INSTAGRAM_USER_ID_BR", "INSTAGRAM_ACCESS_TOKEN_BR"):
        monkeypatch.delenv(v, raising=False)
    app.http = FakeMetaFB()
    page = requests.get(base + "/accounts").text
    assert "Generate Token" in page and "@vicepulse.en" in page and "@vicepulse.brasil" in page
    t = token(page)

    # a token that opens only the EN account is refused for BR
    app.http.accounts = {"17841400000000011": "vicepulse.en"}
    r = requests.post(base + "/accounts/instagram-token", data={"csrf": t, "channel": "br", "token": SYSTEM_TOKEN,
                                                                "ig_user_id": "17841400000000011"})
    assert "does not open @vicepulse.brasil (@vicepulse.en)" in r.text
    assert "Instagram BR saved" not in r.text
    app.http.accounts = {"17841400000000011": "vicepulse.en", "17841400000000022": "vicepulse.brasil"}
    # by expected username from /me/accounts
    r = requests.post(base + "/accounts/instagram-token", data={"csrf": t, "channel": "en", "token": SYSTEM_TOKEN})
    assert "Instagram EN saved: @vicepulse.en (ID 17841400000000011)" in r.text and SYSTEM_TOKEN not in r.text
    r = requests.post(base + "/accounts/instagram-token", data={"csrf": t, "channel": "br", "token": SYSTEM_TOKEN,
                                                                "ig_user_id": "17841400000000022"})
    assert "Instagram BR saved: @vicepulse.brasil" in r.text and "token never expires" in r.text

    monkeypatch.setenv("VICEPULSE_DATA_DIR", str(settings.data_dir))
    pub = make_publisher("instagram", "br")
    assert (pub.user_id, pub.graph) == ("17841400000000022", "https://graph.facebook.com")
    assert accounts.load(settings.data_dir, "instagram_br")["expires_at"] is None

    check = accounts.verify(settings, session=app.http)
    assert check["instagram"]["en"]["matches_expected"] and check["instagram"]["br"]["matches_expected"]
    assert check["instagram"]["br"]["publish_permission"] and check["live_publishing"] == "off"
    assert SYSTEM_TOKEN not in json.dumps(check)
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0


def test_token_without_publish_permission_is_refused(settings):
    from vicepulse import accounts
    with pytest.raises(ValueError, match="not allowed to publish"):
        accounts.instagram_save_token(settings.data_dir, "en", SYSTEM_TOKEN, expected_username="vicepulse.en",
                                      session=FakeMetaFB(can_publish=False))
    with pytest.raises(ValueError, match="does not look like"):
        accounts.instagram_save_token(settings.data_dir, "en", "short", expected_username="vicepulse.en")
    assert accounts.load(settings.data_dir, "instagram_en") == {}



class FakeUnifiedLogin:
    """The reported case: an EAA token whose account only resolves on graph.instagram.com, and an ID that
    graph.facebook.com does not know ('Unsupported get request')."""

    def __init__(self, debug_ids=None):
        self.debug_ids, self.urls = debug_ids, []

    def _resp(self, body, code=200):
        r = requests.Response()
        r.status_code, r._content = code, json.dumps(body).encode()
        return r

    def get(self, url, params=None, timeout=None, headers=None):
        assert "access_token" not in (params or {}) and headers["Authorization"] == "Bearer " + SYSTEM_TOKEN
        self.urls.append(url)
        if url == "https://graph.instagram.com/me":
            if self.debug_ids:
                return self._resp({"error": {"message": "Invalid OAuth access token"}}, 400)
            return self._resp({"user_id": "26000000000000001", "username": "vicepulse.en", "account_type": "BUSINESS"})
        if url.endswith("/debug_token"):
            return self._resp({"data": {"type": "SYSTEM_USER", "granular_scopes": [
                {"scope": "instagram_basic", "target_ids": self.debug_ids or []},
                {"scope": "instagram_content_publish", "target_ids": self.debug_ids or []}]}})
        if url == "https://graph.facebook.com/17841400000000011" and self.debug_ids:
            return self._resp({"id": "17841400000000011", "username": "vicepulse.en"})
        if url.endswith("/content_publishing_limit"):
            return self._resp({"data": [{"quota_usage": 0}]})
        return self._resp({"error": {"message": "Unsupported get request. Object with ID '123' does not exist, "
                                                "cannot be loaded due to missing permission, or does not support this operation"}}, 400)


def test_unified_login_token_with_unknown_id_is_found_on_graph_instagram(settings):
    from vicepulse import accounts
    fake = FakeUnifiedLogin()
    info = accounts.instagram_save_token(settings.data_dir, "en", SYSTEM_TOKEN, ig_user_id="123",
                                         expected_username="vicepulse.en", session=fake)
    assert info["username"] == "vicepulse.en" and info["graph"] == "https://graph.instagram.com"
    assert info["user_id"] == "26000000000000001"
    saved = accounts.load(settings.data_dir, "instagram_en")
    assert saved["expires_at"] is None                      # not an IG-prefixed token: VicePulse does not renew it


def test_system_user_token_found_through_its_own_grants(settings):
    from vicepulse import accounts
    fake = FakeUnifiedLogin(debug_ids=["17841400000000011"])
    info = accounts.instagram_save_token(settings.data_dir, "en", SYSTEM_TOKEN, ig_user_id="999",
                                         expected_username="vicepulse.en", session=fake)
    assert (info["user_id"], info["graph"], info["token_kind"]) == ("17841400000000011", "https://graph.facebook.com",
                                                                    "system_user")


def test_failure_message_explains_every_route_without_the_token(settings):
    from vicepulse import accounts

    class Nothing(FakeUnifiedLogin):
        def get(self, url, params=None, timeout=None, headers=None):
            return self._resp({"error": {"message": "Unsupported get request."}}, 400)

    with pytest.raises(ValueError) as ex:
        accounts.instagram_save_token(settings.data_dir, "en", SYSTEM_TOKEN, ig_user_id="123",
                                      expected_username="vicepulse.en", session=Nothing())
    msg = str(ex.value)
    assert "graph.instagram.com/me" in msg and "debug_token" in msg and "graph.facebook.com/<account 123>" in msg
    assert "graph.instagram.com/<account 123>" in msg and SYSTEM_TOKEN not in msg and "EAA (Facebook" in msg


class FakeSystemUserDirectAssets:
    """The reported case: system-user token, Instagram accounts assigned directly (no Pages),
    debug_token refused without app credentials."""

    APP = ("2385291052288387", "0123456789abcdef0123456789abcdef")

    def __init__(self, business_route=False):
        self.business_route = business_route

    def _resp(self, body, code=200):
        r = requests.Response()
        r.status_code, r._content = code, json.dumps(body).encode()
        return r

    def get(self, url, params=None, timeout=None, headers=None):
        auth = headers["Authorization"]
        if url.startswith("https://graph.instagram.com/"):
            return self._resp({"error": {"message": "Invalid OAuth access token - Cannot parse access token"}}, 401)
        path = url.split("graph.facebook.com/")[1]
        if path == "debug_token":
            if auth != "Bearer " + "|".join(self.APP):
                return self._resp({"error": {"message": "(#100) You must provide an app access token"}}, 400)
            assert params["input_token"] == SYSTEM_TOKEN
            return self._resp({"data": {"type": "SYSTEM_USER", "granular_scopes": [
                {"scope": "instagram_basic", "target_ids": ["17841400000000011", "17841400000000022"]},
                {"scope": "instagram_content_publish", "target_ids": ["17841400000000011", "17841400000000022"]}]}})
        assert auth == "Bearer " + SYSTEM_TOKEN
        if path == "me/accounts":
            return self._resp({"data": []})
        if path == "me/businesses":
            return self._resp({"data": [{"id": "900", "name": "VicePulse"}] if self.business_route else []})
        if path == "900/instagram_business_accounts":
            return self._resp({"data": [{"id": "17841400000000011", "username": "vicepulse.en"},
                                        {"id": "17841400000000022", "username": "vicepulse.brasil"}]})
        if path.endswith("/content_publishing_limit"):
            return self._resp({"data": [{"quota_usage": 0}]})
        names = {"17841400000000011": "vicepulse.en", "17841400000000022": "vicepulse.brasil"}
        if path in names:
            return self._resp({"id": path, "username": names[path]})
        return self._resp({"error": {"message": "Unsupported get request."}}, 400)


def test_direct_assets_without_app_credentials_explains_next_step(settings):
    from vicepulse import accounts
    with pytest.raises(ValueError) as ex:
        accounts.instagram_save_token(settings.data_dir, "en", SYSTEM_TOKEN, expected_username="vicepulse.en",
                                      session=FakeSystemUserDirectAssets())
    msg = str(ex.value)
    assert "no Facebook Page" in msg and "save the Meta app ID and app secret" in msg and SYSTEM_TOKEN not in msg


def test_direct_assets_found_with_app_credentials(settings, conn, server):
    from vicepulse import accounts
    base, app = server
    app.http = FakeSystemUserDirectAssets()
    t = token(requests.get(base + "/accounts").text)
    r = requests.post(base + "/accounts/meta-app", data={"csrf": t, "app_id": FakeSystemUserDirectAssets.APP[0],
                                                         "app_secret": FakeSystemUserDirectAssets.APP[1]})
    assert "Meta app saved" in r.text and FakeSystemUserDirectAssets.APP[1] not in r.text
    r = requests.post(base + "/accounts/instagram-token", data={"csrf": t, "channel": "en", "token": SYSTEM_TOKEN})
    assert "Instagram EN saved: @vicepulse.en (ID 17841400000000011)" in r.text
    r = requests.post(base + "/accounts/instagram-token", data={"csrf": t, "channel": "br", "token": SYSTEM_TOKEN})
    assert "Instagram BR saved: @vicepulse.brasil (ID 17841400000000022)" in r.text
    assert accounts.load(settings.data_dir, "instagram_br")["token_kind"] == "system_user"


def test_direct_assets_found_through_business_portfolio(settings):
    from vicepulse import accounts
    info = accounts.instagram_save_token(settings.data_dir, "br", SYSTEM_TOKEN, expected_username="vicepulse.brasil",
                                         session=FakeSystemUserDirectAssets(business_route=True))
    assert info["user_id"] == "17841400000000022" and info["graph"] == "https://graph.facebook.com"
