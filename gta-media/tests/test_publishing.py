"""Facebook Reels client, error kinds, retry back-off, duplicate guard and rights scopes. Fakes only, no network."""

from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from vicepulse import accounts, db, publish, rights
from vicepulse.platforms import FacebookPublisher, PlatformError, PublishRequest, PublishResult, classify
from vicepulse.platforms.base import check

from test_clients import FakeResponse, FakeSession

FB = "https://graph.facebook.com/v24.0"


def _req(tmp_path, **opts):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\0" * 2048)
    return PublishRequest(file_path=f, title="Huge jump", description="desc", hashtags=["#GTA"], language="en",
                          options=opts)


def test_facebook_reel_upload_as_draft_until_audited(tmp_path):
    http = FakeSession([
        ("POST", "upload_phase=start", FakeResponse(200, {"video_id": "991", "upload_url": "https://rupload.facebook.com/video-upload/v24.0/991"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("POST", "upload_phase=finish", FakeResponse(200, {"success": True})),
        ("GET", "/991", FakeResponse(200, {"status": {"video_status": "processing"}, "permalink_url": "/reel/991"})),
    ])

    # the fake matches on URL fragments, so put the query in the URL the way requests would
    class S(FakeSession):
        def post(self, url, params=None, **kw):
            if params:
                url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
            return self._handle("POST", url, **kw)

        def get(self, url, params=None, **kw):
            return self._handle("GET", url, **kw)

    http = S(http.rules)
    res = FacebookPublisher(page_id="123", access_token="PAGE", session=http).publish(_req(tmp_path, duration_s=20.0))
    assert res.external_id == "991" and res.visibility == "draft" and res.url == "https://www.facebook.com/reel/991"
    finish = [c for c in http.calls if "upload_phase=finish" in c[1]][0]
    assert "video_state=DRAFT" in finish[1] and "Huge jump" in finish[1]
    upload = [c for c in http.calls if "rupload" in c[1]][0]
    assert upload[2]["headers"]["Authorization"] == "OAuth PAGE" and upload[2]["headers"]["file_size"] == "2048"
    assert all("PAGE" not in c[1] or "access_token=PAGE" in c[1] for c in http.calls)

    http2 = S([("POST", "upload_phase=start", FakeResponse(200, {"video_id": "5"})),
               ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
               ("POST", "upload_phase=finish", FakeResponse(200, {"success": True})),
               ("GET", "/5", FakeResponse(500, {}))])
    res = FacebookPublisher(page_id="1", access_token="T", audited=True, session=http2).publish(_req(tmp_path))
    assert res.visibility == "public" and res.url is None          # a failed status call does not undo the post
    assert "video_state=PUBLISHED" in [c for c in http2.calls if "finish" in c[1]][0][1]


def test_facebook_refuses_reels_outside_the_length_limits(tmp_path):
    with pytest.raises(PlatformError, match="3-90 s"):
        FacebookPublisher(page_id="1", access_token="T", session=FakeSession([])).publish(_req(tmp_path, duration_s=95))
    with pytest.raises(PlatformError, match="not configured"):
        FacebookPublisher(page_id="", access_token="T")


@pytest.mark.parametrize("status,body,kind,retry", [
    (401, {}, "auth", False),
    (400, {"error": {"code": 190, "message": "Error validating access token"}}, "auth", False),
    (400, {"error": {"code": 4, "message": "Application request limit reached"}}, "rate_limit", True),
    (403, {"error": {"code": 10, "message": "Permission denied"}}, "permission", False),
    (403, {"error": {"errors": [{"reason": "quotaExceeded"}]}}, "quota", True),
    (429, {}, "rate_limit", True),
    (503, {}, "network", True),
    (400, {"error": {"code": "invalid_params"}}, "rejected", False),
])
def test_errors_are_classified(status, body, kind, retry):
    with pytest.raises(PlatformError) as e:
        check(FakeResponse(status, body), "upload")
    assert (e.value.kind, e.value.retryable) == (kind, retry)


def test_retry_after_and_backoff():
    r = FakeResponse(429, {}, headers={"Retry-After": "120"})
    with pytest.raises(PlatformError) as e:
        check(r, "x")
    assert e.value.retry_after == 120 and publish.retry_delay(1, e.value) == 120
    plain = PlatformError("x", retryable=True, kind="network")
    assert [publish.retry_delay(a, plain) for a in (1, 2, 3)] == [900, 1800, 3600]
    assert publish.retry_delay(1, PlatformError("q", retryable=True, kind="quota")) == publish.QUOTA_WAIT_S


def test_facebook_page_is_found_from_a_pasted_token(tmp_path):
    pages = {"data": [{"id": "11", "name": "Other Page", "access_token": "P11", "tasks": ["ANALYZE"]},
                      {"id": "22", "name": "VicePulse EN", "access_token": "P22", "tasks": ["CREATE_CONTENT", "MANAGE"]}]}
    http = FakeSession([("GET", "/me/accounts", FakeResponse(200, pages))])
    saved = accounts.facebook_save_page(tmp_path, "en", "EAAtoken", expected_name="VicePulse EN", session=http)
    assert saved == {"page_id": "22", "page_name": "VicePulse EN", "tasks": ["CREATE_CONTENT", "MANAGE"],
                     "can_post": True, "connected_at": saved["connected_at"]}
    assert accounts.facebook_credentials(tmp_path, "en") == ("22", "P22")
    with pytest.raises(ValueError, match="not found"):
        accounts.facebook_save_page(tmp_path, "br", "EAAtoken", expected_name="VicePulse BR",
                                    session=FakeSession([("GET", "/me/accounts", FakeResponse(200, pages))]))
    with pytest.raises(ValueError, match="cannot manage any"):
        accounts.facebook_save_page(tmp_path, "br", "EAAtoken",
                                    session=FakeSession([("GET", "/me/accounts", FakeResponse(200, {"data": []}))]))


def test_rights_scopes():
    base = dict(licence_type="written_licence", platforms=["youtube", "facebook"], languages=["*"],
                starts_on=date(2026, 1, 1), expires_on=None, verified_at=datetime.now(timezone.utc),
                document_url="https://x.invalid/l.pdf", derivative_edits_allowed=True, monetisation_allowed=True)
    assert rights.check_grant({**base, "scopes": None}, scope="vicepulse_channel_publishing").ok    # old entries
    assert not rights.check_grant({**base, "scopes": None}, scope="creator_channel_publishing").ok
    only_creator = {**base, "scopes": ["clip_creation", "creator_channel_publishing"]}
    d = rights.check_grant(only_creator, scope="vicepulse_channel_publishing")
    assert not d.ok and "scope vicepulse_channel_publishing" in d.reasons[0]
    d = rights.check_grant({**base, "scopes": ["clip_creation", "vicepulse_channel_publishing"]},
                           scope="vicepulse_channel_publishing", monetised=True)
    assert not d.ok and "monetisation" in d.reasons[0]


# ---------------------------------------------------------------- DB: duplicate guard, connection errors, requeue

def _job(conn, tmp_path, *, platform="youtube", sha="abc", n=1):
    rid = conn.execute("""INSERT INTO rights_ledger (owner_name, licence_type, platforms, starts_on, verified_by,
                          verified_at, monetisation_allowed, derivative_edits_allowed)
                          VALUES ('own', 'own', %s, '2026-01-01', 'test', now(), true, true) RETURNING id""",
                       (["youtube", "instagram", "tiktok", "facebook"],)).fetchone()["id"]
    f = tmp_path / f"c{n}.mp4"
    f.write_bytes(b"x")
    src = conn.execute("""INSERT INTO sources (game, origin, rights_id, file_path, file_sha256, status)
                          VALUES ('gta5', 'own_recording', %s, %s, %s, 'analysed') RETURNING id""",
                       (rid, str(f), f"src{n}")).fetchone()["id"]
    cand = conn.execute("INSERT INTO candidates (source_id, start_s, end_s) VALUES (%s, 0, 10) RETURNING id",
                        (src,)).fetchone()["id"]
    ch = conn.execute("SELECT id FROM channels WHERE key = 'en'").fetchone()["id"]
    titles = {platform: {"title": "t", "description": "d", "hashtags": ["#GTA"]}}
    clip = conn.execute("""INSERT INTO clips (candidate_id, channel_id, language, for_date, in_s, out_s, status, titles)
                           VALUES (%s, %s, 'en', '2026-11-20', 0, 10, 'scheduled', %s) RETURNING id""",
                        (cand, ch, db.Jsonb(titles))).fetchone()["id"]
    r = conn.execute("""INSERT INTO renders (clip_id, platform, file_path, file_sha256, width, height, duration_s,
                        has_voice, has_captions) VALUES (%s, %s, %s, %s, 720, 1280, 10, false, true) RETURNING id""",
                     (clip, platform, str(f), sha)).fetchone()["id"]
    acct = conn.execute("SELECT id FROM platform_accounts WHERE channel_id = %s AND platform = %s",
                        (ch, platform)).fetchone()["id"]
    conn.execute("UPDATE platform_accounts SET enabled = true WHERE id = %s", (acct,))
    job = conn.execute("""INSERT INTO publish_jobs (clip_id, platform, account_id, render_id, scheduled_for)
                          VALUES (%s, %s, %s, %s, '2026-11-20T12:00Z') RETURNING id""",
                       (clip, platform, acct, r)).fetchone()["id"]
    conn.commit()
    return job


class Pub:
    def __init__(self, log, exc=None):
        self.log, self.exc = log, exc

    def publish(self, req):
        self.log.append(req.file_path.name)
        if self.exc:
            raise self.exc
        return PublishResult("id-" + req.file_path.stem, "https://example.invalid/x")


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def test_same_file_is_never_posted_twice_to_one_account(conn, settings, tmp_path):
    settings.allow_live_publish = True
    a = _job(conn, tmp_path, n=1)
    b = _job(conn, tmp_path, n=2)                  # a different clip row with the identical file
    log = []
    out = publish.run_due(conn, settings, now=NOW, publisher_factory=lambda *a, **k: Pub(log))
    assert out["published"] == [a] and out["blocked"] == [b] and len(log) == 1
    row = conn.execute("SELECT error_kind, last_error FROM publish_jobs WHERE id = %s", (b,)).fetchone()
    assert row["error_kind"] == "duplicate" and f"job {a}" in row["last_error"]


def test_connection_errors_retry_only_when_nothing_can_have_been_sent(conn, settings, tmp_path):
    settings.allow_live_publish = True
    a = _job(conn, tmp_path, n=1, sha="h1")
    log = []
    out = publish.run_due(conn, settings, now=NOW,
                          publisher_factory=lambda *x, **k: Pub(log, requests.ConnectTimeout("no route")))
    assert out["retry"] == [a]
    out = publish.run_due(conn, settings, now=NOW + timedelta(hours=1),
                          publisher_factory=lambda *x, **k: Pub(log, requests.ReadTimeout("mid-upload")))
    assert out["failed"] == [a]
    row = conn.execute("SELECT status, error_kind, last_error FROM publish_jobs WHERE id = %s", (a,)).fetchone()
    assert row["error_kind"] == "network" and "check the platform" in row["last_error"]
    # a person checked: the post is not there -> back in the queue, then it goes out once
    with pytest.raises(ValueError):
        publish.requeue(conn, 99999, reason="x")
    publish.requeue(conn, a, reason="checked YouTube Studio, not uploaded", when=NOW + timedelta(hours=2))
    out = publish.run_due(conn, settings, now=NOW + timedelta(hours=2), publisher_factory=lambda *x, **k: Pub(log))
    assert out["published"] == [a]
    with pytest.raises(ValueError, match="published"):
        publish.requeue(conn, a, reason="again")
    rep = publish.queue_report(conn)
    assert rep["counts"]["youtube"] == {"published": 1}


def test_scopes_block_publishing_when_the_creator_only_allowed_their_own_channel(conn, settings, tmp_path):
    settings.allow_live_publish = True
    a = _job(conn, tmp_path, n=1, sha="s1")
    conn.execute("UPDATE rights_ledger SET scopes = %s", (["clip_creation", "creator_channel_publishing"],))
    conn.commit()
    out = publish.run_due(conn, settings, now=NOW, publisher_factory=lambda *x, **k: Pub([]))
    assert out["blocked"] == [a]
    assert "vicepulse_channel_publishing" in conn.execute("SELECT last_error FROM publish_jobs").fetchone()["last_error"]
