"""TikTok: Login Kit connection from the review page, token renewal, and the guideline-following post screen.

Everything runs against a fake TikTok; nothing reaches the network.
"""

from __future__ import annotations

import json
import re

import pytest
import requests

from test_review_page import made_clip, server, token  # noqa: F401  (pytest fixtures)


class FakeTikTok:
    """Stands in for TikTok's OAuth and Content Posting endpoints; records every call."""

    def __init__(self, username="vicepulse.en", scope="user.info.basic,video.publish"):
        self.calls, self.username, self.scope, self.n = [], username, scope, 0

    def _resp(self, body, code=200):
        r = requests.Response()
        r.status_code, r._content = code, json.dumps(body).encode()
        return r

    def post(self, url, data=None, json=None, headers=None, timeout=None, params=None):
        self.calls.append(("post", url, dict(data or {}), json))
        if url.endswith("/v2/oauth/token/"):
            self.n += 1
            if data["grant_type"] == "refresh_token":
                assert data["refresh_token"] == "TT-RT-1"
            return self._resp({"access_token": f"TT-AT-{self.n}", "refresh_token": f"TT-RT-{self.n}",
                               "expires_in": 7200, "refresh_expires_in": 15552000, "open_id": "oid",
                               "scope": self.scope, "token_type": "Bearer"})
        assert headers["Authorization"].startswith("Bearer TT-AT-")
        if url.endswith("/creator_info/query/"):
            return self._resp({"data": {"creator_username": self.username, "creator_nickname": "VicePulse EN",
                                        "creator_avatar_url": "https://p16.example/a.jpg",
                                        "privacy_level_options": ["FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS",
                                                                  "SELF_ONLY"],
                                        "comment_disabled": False, "duet_disabled": True, "stitch_disabled": True,
                                        "max_video_post_duration_sec": 600},
                               "error": {"code": "ok", "message": ""}})
        if url.endswith("/video/init/"):
            return self._resp({"data": {"publish_id": "v_pub_file~v2.123", "upload_url": "https://upload.example/u"},
                               "error": {"code": "ok"}})
        if url.endswith("/status/fetch/"):
            return self._resp({"data": {"status": "PUBLISH_COMPLETE"}, "error": {"code": "ok"}})
        raise AssertionError(url)

    def put(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("put", url, {"bytes": len(data)}, headers))
        return self._resp({})


REDIRECT = "https://localhost:8090/oauth/tiktok/callback"
SECRET = "tt-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN_EN", "TIKTOK_ACCESS_TOKEN_BR",
              "VICEPULSE_TIKTOK_REDIRECT", "VICEPULSE_TIKTOK_TEST_POSTS", "VICEPULSE_REVIEW_URL"):
        monkeypatch.delenv(v, raising=False)


def test_connect_tiktok_from_the_page(settings, conn, server, monkeypatch):
    import os
    from urllib.parse import parse_qs as pq, urlparse as up
    from vicepulse import accounts
    from vicepulse.platforms import make_publisher
    base, app = server
    app.http = FakeTikTok()
    page = requests.get(base + "/accounts").text
    assert REDIRECT in page and "Client key" in page
    t = token(page)
    assert "client key looks wrong" in requests.post(base + "/accounts/tiktok-client",
                                                     data={"csrf": t, "client_key": "a b", "client_secret": "s" * 32}).text
    r = requests.post(base + "/accounts/tiktok-client", data={"csrf": t, "client_key": "awtestkey123",
                                                              "client_secret": SECRET})
    assert "TikTok app saved" in r.text and SECRET not in r.text and "Connect TikTok" in r.text

    r = requests.post(base + "/accounts/tiktok-start", data={"csrf": t, "channel": "en"}, allow_redirects=False)
    loc = up(r.headers["Location"])
    q = {k: v[0] for k, v in pq(loc.query).items()}
    assert loc.netloc == "www.tiktok.com" and loc.path == "/v2/auth/authorize/" and q["client_key"] == "awtestkey123"
    assert q["scope"] == "user.info.basic,video.publish" and q["response_type"] == "code"
    assert q["redirect_uri"] == REDIRECT

    r = requests.post(base + "/accounts/tiktok-paste", data={
        "csrf": t, "url": f"{REDIRECT}?code=CODE%2A1&scopes=video.publish&state={q['state']}"})
    assert "TikTok connected for EN: VicePulse EN @vicepulse.en" in r.text and "TT-AT-1" not in r.text
    tok = [c for c in app.http.calls if c[1].endswith("/v2/oauth/token/")][0][2]
    assert tok["code"] == "CODE*1" and tok["redirect_uri"] == REDIRECT and tok["client_secret"] == SECRET
    f = settings.data_dir / "secrets" / "tiktok_en.json"
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"
    assert accounts.tiktok_credentials(settings.data_dir, "en") == "TT-AT-1"
    monkeypatch.setenv("VICEPULSE_DATA_DIR", str(settings.data_dir))
    assert make_publisher("tiktok", "en").token == "TT-AT-1"
    assert "@vicepulse.en" in requests.get(base + "/accounts").text

    # a reused or forged state is refused
    r = requests.post(base + "/accounts/tiktok-paste", data={"csrf": t, "url": f"{REDIRECT}?code=X&state={q['state']}"})
    assert "expired" in r.text
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0


def test_tiktok_refuses_missing_scope_and_wrong_account_and_rotates_refresh_token(settings):
    import time as _t
    from vicepulse import accounts
    accounts.save_tiktok_client(settings.data_dir, "awtestkey123", SECRET)
    with pytest.raises(ValueError, match="did not grant video.publish"):
        accounts.tiktok_connect(settings.data_dir, "en", "c", REDIRECT, session=FakeTikTok(scope="user.info.basic"))
    with pytest.raises(ValueError, match="expects @vicepulse.en"):
        accounts.tiktok_connect(settings.data_dir, "en", "c", REDIRECT, expected_username="vicepulse.en",
                                session=FakeTikTok(username="someone.else"))
    assert not (settings.data_dir / "secrets" / "tiktok_en.json").exists()

    fake = FakeTikTok()
    accounts.tiktok_connect(settings.data_dir, "en", "c", REDIRECT, session=fake)
    info = accounts.load(settings.data_dir, "tiktok_en")
    info["expires_at"] = int(_t.time()) + 60          # nearly expired -> renewed, and the new refresh token is kept
    accounts.save(settings.data_dir, "tiktok_en", info)
    assert accounts.tiktok_credentials(settings.data_dir, "en", session=fake) == "TT-AT-2"
    assert accounts.load(settings.data_dir, "tiktok_en")["refresh_token"] == "TT-RT-2"
    assert accounts.tiktok_credentials(settings.data_dir, "en", session=fake) == "TT-AT-2"   # still fresh: no call
    assert sum(1 for c in fake.calls if c[1].endswith("/v2/oauth/token/")) == 2


def test_tiktok_post_screen_follows_the_guidelines_and_posts_privately(settings, conn, made_clip, server, monkeypatch):
    from vicepulse import accounts
    base, app = server
    accounts.save_tiktok_client(settings.data_dir, "awtestkey123", SECRET)
    app.http = FakeTikTok()
    accounts.tiktok_connect(settings.data_dir, "en", "c", REDIRECT, session=app.http)

    assert "approve it first" in requests.get(f"{base}/tiktok/post/{made_clip}").text
    page = requests.get(base + "/").text
    requests.post(base + "/decide", data={"csrf": token(page), "clip": made_clip, "decision": "approve"})
    assert f"/tiktok/post/{made_clip}" in requests.get(base + "/done").text

    page = requests.get(f"{base}/tiktok/post/{made_clip}").text
    assert "Posting to: VicePulse EN" in page and "@vicepulse.en" in page and f"/media/{made_clip}/" in page
    assert '<option value="" selected disabled>Choose who can view</option>' in page
    assert '<option value="SELF_ONLY">Only me</option>' in page and 'value="FOLLOWER_OF_CREATOR" disabled' in page
    assert " checked" not in page                                              # nothing ticked by default
    assert "name=allow_duet value=1 disabled" in page                         # this account has Duet off
    assert "music-usage-confirmation" in page and "bc-policy" in page and "Disclose video content" in page
    assert "Test posting is switched off" in page and "few minutes" in page
    form = {"csrf": token(page), "clip": made_clip, "caption": "My edited caption #GTA",
            "privacy_level": "SELF_ONLY", "allow_comment": "1"}

    def inits():
        return [c for c in app.http.calls if c[1].endswith("/video/init/")]

    r = requests.post(base + "/tiktok/post", data=form)                       # switch off -> nothing sent
    assert "switched off" in r.text and not inits()

    monkeypatch.setenv("VICEPULSE_TIKTOK_TEST_POSTS", "1")
    assert "(private) posts are allowed" in requests.post(base + "/tiktok/post",
                                                          data={**form, "privacy_level": "FOLLOWER_OF_CREATOR"}).text
    assert "choose who can view" in requests.post(base + "/tiktok/post", data={**form, "privacy_level": ""}).text
    assert "Your brand and/or Branded content" in requests.post(base + "/tiktok/post",
                                                                data={**form, "disclose": "1"}).text
    assert "be posted as a test" in requests.post(base + "/tiktok/post",
                                                  data={**form, "disclose": "1", "branded_content": "1"}).text
    assert not inits()

    r = requests.post(base + "/tiktok/post", data=form)
    assert "Uploaded to TikTok as a private" in r.text and "Done. The video is on the account" in r.text
    init = inits()[0][3]
    pi = init["post_info"]
    assert pi["title"] == "My edited caption #GTA" and pi["privacy_level"] == "SELF_ONLY"
    assert pi["disable_comment"] is False and pi["disable_duet"] is True and pi["disable_stitch"] is True
    assert pi["brand_content_toggle"] is False and init["source_info"]["source"] == "FILE_UPLOAD"
    assert any(c[0] == "put" for c in app.http.calls)
    conn.rollback()
    ev = conn.execute("SELECT outcome, detail FROM events WHERE stage = 'tiktok_test_post'").fetchall()
    assert [x["outcome"] for x in ev] == ["uploaded"] and ev[0]["detail"]["privacy"] == "SELF_ONLY"
    assert conn.execute("SELECT count(*) AS n FROM publish_jobs").fetchone()["n"] == 0
    assert "bad publish id" in requests.get(base + "/tiktok/status", params={"channel": "en", "id": "x y"}).text
    st = requests.get(base + "/tiktok/status", params={"channel": "en", "id": "v_pub_file~v2.123"}).text
    assert re.search(r"Status: PUBLISH_COMPLETE", st) and "location.reload" not in st   # finished: stops refreshing
