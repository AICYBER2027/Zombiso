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
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd.RequestHandlerClass
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
