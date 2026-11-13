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
