"""Platform clients and the Claude client, exercised only through fakes. No network."""

import json
from types import SimpleNamespace

import pytest

from vicepulse.config import Settings
from vicepulse.llm import SCRIPT_SCHEMA, ClaudeLLM, LLMRefusal
from vicepulse.platforms import (InstagramPublisher, PlatformError, PublishRequest, TikTokPublisher,
                                 YouTubePublisher)
from vicepulse.platforms.tiktok import chunk_plan


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    """Records every request and answers from a list of (method, url-fragment, response) rules."""

    def __init__(self, rules):
        self.rules, self.calls = list(rules), []

    def _handle(self, method, url, **kw):
        if hasattr(kw.get("data"), "read"):
            kw["data"] = kw["data"].read()
        self.calls.append((method, url, kw))
        for i, (m, frag, resp) in enumerate(self.rules):
            if m == method and frag in url:
                if not isinstance(resp, FakeResponse):       # a queue of responses
                    resp = resp.pop(0)
                return resp
        raise AssertionError(f"unexpected request {method} {url}")

    def get(self, url, **kw): return self._handle("GET", url, **kw)
    def post(self, url, **kw): return self._handle("POST", url, **kw)
    def put(self, url, **kw): return self._handle("PUT", url, **kw)
    def delete(self, url, **kw): return self._handle("DELETE", url, **kw)


@pytest.fixture()
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 2048)
    return p


def request(video, **kw):
    return PublishRequest(file_path=video, title="Nobody expected this", description="Body text",
                          hashtags=["#GTA", "#VicePulse"], language="en", **kw)


# ------------------------------------------------------------------ YouTube

def yt_session():
    return FakeSession([
        ("POST", "oauth2.googleapis.com/token", FakeResponse(body={"access_token": "tok"})),
        ("POST", "upload/youtube/v3/videos", FakeResponse(headers={"Location": "https://upload.example.invalid/session1"})),
        ("PUT", "upload.example.invalid/session1", FakeResponse(body={"id": "abc123", "status": {"privacyStatus": "private"}})),
        ("DELETE", "youtube/v3/videos", FakeResponse(status=204)),
        ("GET", "youtubeanalytics", FakeResponse(body={"rows": [[1000, 50, 62.5, 40, 3, 7, 12]]})),
    ])


def test_youtube_uploads_private_until_audited(video):
    s = yt_session()
    yt = YouTubePublisher(client_id="id", client_secret="secret", refresh_token="rt", audited=False, session=s)
    result = yt.publish(request(video))
    assert result.external_id == "abc123" and result.url.endswith("/shorts/abc123")
    start = next(c for c in s.calls if "upload/youtube" in c[1])
    assert start[2]["params"] == {"uploadType": "resumable", "part": "snippet,status"}
    body = start[2]["json"]
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}
    assert body["snippet"]["categoryId"] == "20" and body["snippet"]["tags"] == ["GTA", "VicePulse"]
    assert next(c for c in s.calls if c[0] == "PUT")[2]["data"] == video.read_bytes()


def test_youtube_public_when_audited_and_discloses_synthetic_voice(video):
    s = yt_session()
    YouTubePublisher(client_id="id", client_secret="s", refresh_token="rt", audited=True, session=s).publish(
        request(video, synthetic_media=True))
    status = next(c for c in s.calls if "upload/youtube" in c[1])[2]["json"]["status"]
    assert status["privacyStatus"] == "public" and status["containsSyntheticMedia"] is True


def test_youtube_metrics_and_takedown(video):
    yt = YouTubePublisher(client_id="id", client_secret="s", refresh_token="rt", session=yt_session())
    m = yt.metrics("abc123")
    assert m["views"] == 1000 and m["watch_time_s"] == 3000 and m["followers_delta"] == 12
    assert yt.take_down("abc123") is True


def test_youtube_needs_credentials():
    with pytest.raises(PlatformError):
        YouTubePublisher(client_id="", client_secret="", refresh_token="")


def test_rate_limit_is_retryable(video):
    s = FakeSession([("POST", "oauth2.googleapis.com/token", FakeResponse(status=429, body={"error": "slow down"}))])
    with pytest.raises(PlatformError) as e:
        YouTubePublisher(client_id="id", client_secret="s", refresh_token="rt", session=s).publish(request(video))
    assert e.value.retryable


# ------------------------------------------------------------------ Instagram

def ig_session(quota_usage=3, statuses=("IN_PROGRESS", "FINISHED")):
    return FakeSession([
        ("GET", "content_publishing_limit", FakeResponse(body={"data": [{"quota_usage": quota_usage, "config": {"quota_total": 50}}]})),
        ("POST", "/1789/media_publish", FakeResponse(body={"id": "media9"})),
        ("POST", "/1789/media", FakeResponse(body={"id": "container7"})),
        ("GET", "/container7", [FakeResponse(body={"status_code": s}) for s in statuses]),
        ("GET", "/media9", FakeResponse(body={"permalink": "https://www.instagram.com/reel/xyz/"})),
    ])


def test_instagram_container_then_publish(video):
    s = ig_session()
    ig = InstagramPublisher(user_id="1789", access_token="tok", session=s, poll_seconds=0)
    result = ig.publish(request(video, public_url="https://media.example.invalid/renders/en/clip1_instagram.mp4"))
    assert result.external_id == "media9" and "instagram.com/reel" in result.url
    create = next(c for c in s.calls if c[1].endswith("/1789/media"))[2]["data"]
    assert create["media_type"] == "REELS" and create["video_url"].startswith("https://")
    assert "#GTA" in create["caption"]
    assert [c[1].rsplit("/", 1)[-1] for c in s.calls if c[0] == "POST"] == ["media", "media_publish"]


def test_instagram_refuses_without_public_url_or_quota(video):
    ig = InstagramPublisher(user_id="1789", access_token="tok", session=ig_session(quota_usage=50), poll_seconds=0)
    with pytest.raises(PlatformError, match="public https URL"):
        ig.publish(request(video))
    with pytest.raises(PlatformError, match="limit reached"):
        ig.publish(request(video, public_url="https://media.example.invalid/x.mp4"))


def test_instagram_container_error_and_manual_takedown(video):
    ig = InstagramPublisher(user_id="1789", access_token="tok", session=ig_session(statuses=("ERROR",)), poll_seconds=0)
    with pytest.raises(PlatformError, match="ERROR"):
        ig.publish(request(video, public_url="https://media.example.invalid/x.mp4"))
    assert ig.take_down("media9") is False


# ------------------------------------------------------------------ TikTok

TT_OPTIONS = {"privacy_level": "SELF_ONLY", "allow_comment": True, "allow_duet": False, "allow_stitch": False,
              "consent_confirmed": True}


def tt_session(privacy_options=("PUBLIC_TO_EVERYONE", "SELF_ONLY")):
    return FakeSession([
        ("POST", "creator_info/query", FakeResponse(body={"data": {"privacy_level_options": list(privacy_options),
                                                                    "duet_disabled": False, "comment_disabled": False}})),
        ("POST", "video/init", FakeResponse(body={"data": {"publish_id": "p_1", "upload_url": "https://upload.example.invalid/tt"}})),
        ("PUT", "upload.example.invalid/tt", FakeResponse(status=201)),
    ])


def test_tiktok_direct_post_uses_reviewer_choices(video):
    s = tt_session()
    result = TikTokPublisher(access_token="tok", session=s).publish(request(video, options=TT_OPTIONS))
    assert result.external_id == "p_1" and result.visibility == "SELF_ONLY"
    init = next(c for c in s.calls if "video/init" in c[1])[2]["json"]
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 2048, "chunk_size": 2048, "total_chunk_count": 1}
    info = init["post_info"]
    assert info["privacy_level"] == "SELF_ONLY" and info["disable_comment"] is False
    assert info["disable_duet"] is True and info["disable_stitch"] is True and info["is_aigc"] is False
    put = next(c for c in s.calls if c[0] == "PUT")[2]
    assert put["headers"]["Content-Range"] == "bytes 0-2047/2048" and len(put["data"]) == 2048


@pytest.mark.parametrize("options, message", [
    ({}, "reviewer's choices"),
    ({**TT_OPTIONS, "consent_confirmed": False}, "consent"),
    ({**TT_OPTIONS, "privacy_level": "PUBLIC_TO_EVERYONE"}, "unaudited"),
])
def test_tiktok_refuses_without_choices_consent_or_audit(video, options, message):
    s = tt_session()
    with pytest.raises(PlatformError, match=message):
        TikTokPublisher(access_token="tok", audited=False, session=s).publish(request(video, options=options))
    assert s.calls == []                    # refused before any request was sent


def test_tiktok_privacy_must_be_offered_by_account(video):
    with pytest.raises(PlatformError, match="not offered"):
        TikTokPublisher(access_token="tok", session=tt_session(privacy_options=("FOLLOWER_OF_CREATOR",))).publish(
            request(video, options=TT_OPTIONS))


def test_tiktok_chunk_plan():
    mb = 1024 * 1024
    assert chunk_plan(3 * mb) == (3 * mb, 1)
    assert chunk_plan(10 * mb) == (10 * mb, 1)
    assert chunk_plan(25 * mb) == (10 * mb, 2)      # last chunk carries the extra 5 MB
    assert TikTokPublisher(access_token="tok").take_down("p_1") is False


# ------------------------------------------------------------------ Claude client

class FakeMessages:
    def __init__(self, log, name, stop_reason="end_turn", text="{}"):
        self.log, self.name, self.stop_reason, self.text = log, name, stop_reason, text

    def create(self, **params):
        self.log.append((self.name, params))
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=None,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=self.text)])


def fake_client(log, **kw):
    return SimpleNamespace(messages=FakeMessages(log, "messages", **kw),
                           beta=SimpleNamespace(messages=FakeMessages(log, "beta", **kw)))


def test_claude_requests_use_agreed_models_and_structured_output(tmp_path):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"jpegbytes")
    log = []
    script = {"hook": "h", "body": "b", "closer": "c", "angle": "a",
              **{p: {"title": "t", "description": "d", "hashtags": []} for p in ("youtube", "instagram", "tiktok")}}
    llm = ClaudeLLM(Settings(), client=fake_client(log, text=json.dumps(script)))
    channel = {"name": "VicePulse BR", "language": "pt-BR", "voice_style": "x", "hashtags": ["#GTA"], "key": "br"}
    out = llm.script(channel, {"id": 1, "in_s": 0, "out_s": 20, "selection_reason": "r"}, [frame], game="gta5")
    assert out["hook"] == "h"
    name, params = log[-1]
    assert name == "beta" and params["model"] == "claude-opus-5-5"
    assert params["betas"] == ["server-side-fallback-2026-07-01"] and params["fallbacks"] == "default"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"]["format"] == {"type": "json_schema", "schema": SCRIPT_SCHEMA}
    assert not {"temperature", "top_p", "tool_choice", "tools"} & set(params)
    assert params["messages"][0]["content"][0]["type"] == "image"
    assert "pt-BR" in params["messages"][0]["content"][-1]["text"]

    llm = ClaudeLLM(Settings(), client=fake_client(log, text=json.dumps(
        {"hook_score": 7, "clarity_score": 6, "policy_tags": [], "summary": "s"})))
    llm.triage({"id": 1, "start_s": 0, "end_s": 8, "signal_score": 2, "transcript": None}, [frame], game="gta5")
    name, params = log[-1]
    assert name == "messages" and params["model"] == "claude-haiku-5-5" and "fallbacks" not in params


def test_claude_refusal_and_truncation_are_errors():
    log = []
    with pytest.raises(LLMRefusal):
        ClaudeLLM(Settings(), client=fake_client(log, stop_reason="refusal")).final_check(
            {"name": "x", "language": "en"}, {"script": {}}, [], game="gta5", block_tags=[])
    with pytest.raises(RuntimeError, match="max_tokens"):
        ClaudeLLM(Settings(), client=fake_client(log, stop_reason="max_tokens")).final_check(
            {"name": "x", "language": "en"}, {"script": {}}, [], game="gta5", block_tags=[])


def test_structured_output_schemas_are_strict():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    from vicepulse import llm
    for schema in (llm.TRIAGE_SCHEMA, llm.SELECT_SCHEMA, llm.SCRIPT_SCHEMA, llm.CHECK_SCHEMA):
        walk(schema)
