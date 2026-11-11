"""Priority 1: creator discovery, monitoring, ranking and permission tracking. Fake APIs only; no network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import make_footage
from test_clients import FakeResponse
from vicepulse import discovery, ingest
from vicepulse.creator_policy import scan
from vicepulse.sources import QuotaExceeded, SourceError, iso_duration_s, twitch_duration_s
from vicepulse.sources.twitch import Twitch
from vicepulse.sources.youtube_data import COST, YouTubeData

NOW = datetime.now(timezone.utc)
CFG = discovery.load_config()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ fake YouTube Data API

def yt_channel(cid, title, subs, *, handle=None, country="US", desc=""):
    return {"id": cid, "snippet": {"title": title, "customUrl": handle, "country": country, "description": desc},
            "statistics": {"subscriberCount": str(subs), "viewCount": str(subs * 100), "videoCount": "500"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}, "brandingSettings": {"channel": {}}}


def yt_video(vid, cid, title, *, views=100000, likes=5000, comments=500, hours_ago=5, live=None, desc=""):
    v = {"id": vid, "snippet": {"channelId": cid, "title": title, "description": desc, "tags": [],
                                "publishedAt": iso(NOW - timedelta(hours=hours_ago)), "defaultAudioLanguage": "en"},
         "statistics": {"viewCount": str(views), "likeCount": str(likes), "commentCount": str(comments)},
         "contentDetails": {"duration": "PT12M30S"}}
    if live:
        v["liveStreamingDetails"] = live
    return v


class FakeYouTube:
    def __init__(self):
        self.channels = {
            "UCspeed000000000000000000": yt_channel("UCspeed000000000000000000", "IShowSpeed", 40_000_000, handle="@ishowspeed",
                                                    desc="Speed. Business inquiries: speed@example.invalid"),
            "UCbr00000000000000000000": yt_channel("UCbr00000000000000000000", "GTA Brasil Clips", 900_000, handle="@gtabrasil",
                                                   country="BR", desc="Pode clipar, só dar créditos! Feel free to clip."),
            "UCnoclip0000000000000000": yt_channel("UCnoclip0000000000000000", "No Reupload Gaming", 2_000_000,
                                                   handle="@noreupload", desc="Do not reupload my videos. All rights reserved."),
            "UCtiny000000000000000000": yt_channel("UCtiny000000000000000000", "Tiny GTA Fan", 1_200, handle="@tiny"),
        }
        self.videos = {
            "vidSpeedLive": yt_video("vidSpeedLive", "UCspeed000000000000000000", "GTA 6 FIRST LOOK LIVE",
                                     live={"actualStartTime": iso(NOW - timedelta(hours=1)), "concurrentViewers": "250000"}),
            "vidSpeedUp": yt_video("vidSpeedUp", "UCspeed000000000000000000", "GTA 6 day one stream", views=0, likes=0, comments=0,
                                   live={"scheduledStartTime": iso(NOW + timedelta(days=8))}),
            "vidBr1": yt_video("vidBr1", "UCbr00000000000000000000", "GTA 6: momentos mais engraçados", views=600_000, hours_ago=10),
            "vidNo1": yt_video("vidNo1", "UCnoclip0000000000000000", "GTA VI trailer breakdown", views=300_000, hours_ago=30),
            "vidTiny": yt_video("vidTiny", "UCtiny000000000000000000", "my gta 6 thoughts", views=200),
            "vidSpeedOld": yt_video("vidSpeedOld", "UCspeed000000000000000000", "Speed plays FIFA", views=5_000_000, hours_ago=48),
        }
        self.uploads = {"UUspeed000000000000000000": ["vidSpeedLive", "vidSpeedUp", "vidSpeedOld"],
                        "UUbr00000000000000000000": ["vidBr1"], "UUnoclip0000000000000000": ["vidNo1"]}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kw):
        res = urlparse(url).path.rsplit("/", 1)[-1]
        self.calls.append((res, dict(params)))
        assert params.get("key") == "test-key"
        if res == "channels":
            if "forHandle" in params:
                items = [c for c in self.channels.values() if (c["snippet"]["customUrl"] or "").lstrip("@") == params["forHandle"].lower()]
            else:
                items = [self.channels[i] for i in params["id"].split(",") if i in self.channels]
            return FakeResponse(body={"items": items})
        if res == "search":
            assert params["type"] == "video"
            ev = params.get("eventType")
            pick = {"live": ["vidSpeedLive"], "upcoming": ["vidSpeedUp"]}.get(ev, ["vidBr1", "vidNo1", "vidTiny"])
            return FakeResponse(body={"items": [{"id": {"videoId": v}} for v in pick]})
        if res == "videos":
            return FakeResponse(body={"items": [self.videos[i] for i in params["id"].split(",") if i in self.videos]})
        if res == "playlistItems":
            return FakeResponse(body={"items": [{"contentDetails": {"videoId": v}} for v in self.uploads.get(params["playlistId"], [])]})
        raise AssertionError(res)


# ------------------------------------------------------------------ fake Twitch Helix

class FakeTwitch:
    def __init__(self):
        self.live = {"100": {"id": "s1", "user_id": "100", "user_login": "kaicenat", "game_name": "Grand Theft Auto V",
                             "title": "GTA RP with the boys", "viewer_count": 80000, "language": "en",
                             "started_at": iso(NOW - timedelta(hours=2))},
                     "300": {"id": "s3", "user_id": "300", "user_login": "riogamer", "game_name": "Grand Theft Auto V",
                             "title": "GTA RP Cidade Alta", "viewer_count": 12000, "language": "pt",
                             "started_at": iso(NOW - timedelta(minutes=30))},
                     "400": {"id": "s4", "user_id": "400", "user_login": "smallfry", "game_name": "Grand Theft Auto V",
                             "title": "chill gta", "viewer_count": 12, "language": "en", "started_at": iso(NOW)}}
        self.users = {"100": {"id": "100", "login": "kaicenat", "display_name": "KaiCenat", "description": "no reuploads"},
                      "300": {"id": "300", "login": "riogamer", "display_name": "RioGamer",
                              "description": "Clipes liberados! Join my clipping program on Discord."},
                      "400": {"id": "400", "login": "smallfry", "display_name": "smallfry", "description": ""}}
        self.calls = []
        self.tokens = 0

    def post(self, url, data=None, **kw):
        self.tokens += 1
        return FakeResponse(body={"access_token": "app-token", "expires_in": 3600})

    def get(self, url, params=None, headers=None, **kw):
        path = urlparse(url).path.replace("/helix/", "")
        assert headers["Client-Id"] == "cid" and headers["Authorization"] == "Bearer app-token"
        q = parse_qs("&".join(f"{k}={v}" for k, v in (params.items() if isinstance(params, dict) else params)))
        self.calls.append((path, q))
        if path == "games":
            return FakeResponse(body={"data": [{"id": "32982" if "V" == q["name"][0][-1] else "999"}]
                                      if q["name"][0] == "Grand Theft Auto V" else []})
        if path == "streams":
            if "game_id" in q:
                lang = q.get("language", [None])[0]
                return FakeResponse(body={"data": [s for s in self.live.values() if lang in (None, s["language"])]})
            return FakeResponse(body={"data": [self.live[u] for u in q["user_id"] if u in self.live]})
        if path == "users":
            ids = q.get("id", []) + [u["id"] for u in self.users.values() if u["login"] in q.get("login", [])]
            return FakeResponse(body={"data": [self.users[i] for i in ids if i in self.users]})
        if path == "channels/followers":
            return FakeResponse(status=401, body={"message": "user token required"})
        if path == "schedule":
            if q["broadcaster_id"][0] == "100":
                return FakeResponse(body={"data": {"segments": [{"id": "seg1", "title": "GTA 6 launch night",
                                                                 "start_time": iso(NOW + timedelta(days=8)),
                                                                 "category": {"name": "Grand Theft Auto VI"}}]}})
            return FakeResponse(status=404, body={"message": "not found"})
        if path == "videos":
            return FakeResponse(body={"data": []})
        raise AssertionError(path)


# ------------------------------------------------------------------ pure parts

def test_policy_scanner():
    r = scan([{"source": "s", "url": "u", "text": "Feel free to clip my streams, just give credit!"}])
    assert r.verdict == "stated_allow" and r.conditions == ["credit_required"] and r.evidence[0]["url"] == "u"
    assert scan([{"text": "Please don't clip me or reupload my stuff."}]).verdict == "stated_deny"
    assert scan([{"text": "Join my clipping program on Discord"}]).verdict == "program_available"
    assert scan([{"text": "Feel free to clip"}, {"text": "Do not reupload full streams"}]).verdict == "conflicting"
    assert scan([{"text": "Pode clipar, só dar créditos"}]).verdict == "stated_allow"
    assert scan([{"text": "Business inquiries: x@example.invalid"}]).verdict == "unknown"
    assert scan([]).verdict == "unknown"


def test_game_detection_and_durations():
    assert discovery.detect_game("GTA 6 FIRST LOOK", CFG) == "gta6"
    assert discovery.detect_game("Grand Theft Auto VI trailer", CFG) == "gta6"
    assert discovery.detect_game("GTA RP night", CFG) == "gta5"
    assert discovery.detect_game("GTA V heists", CFG) == "gta5"
    assert discovery.detect_game("Minecraft", CFG) == "other"
    assert iso_duration_s("PT1H2M3S") == 3723 and iso_duration_s("P1DT1S") == 86401 and iso_duration_s(None) is None
    assert twitch_duration_s("1h2m3s") == 3723 and twitch_duration_s("45s") == 45 and twitch_duration_s("") is None


def test_score_rewards_viral_gta_live_creators():
    w = CFG["score_weights"]
    big_live = discovery.score_creator(
        [{"followers": 10_000_000}],
        [{"kind": "upload", "game": "gta6", "views": 3_000_000, "likes": 200_000, "comments": 20_000,
          "published_at": NOW - timedelta(hours=6)},
         {"kind": "live", "game": "gta6", "live_viewers": 200_000, "actual_start": NOW}], w)
    quiet = discovery.score_creator(
        [{"followers": 10_000_000}],
        [{"kind": "upload", "game": "other", "views": 20_000, "likes": 100, "comments": 5,
          "published_at": NOW - timedelta(hours=100)}], w)
    assert 0 <= quiet["total"] < big_live["total"] <= 100
    assert big_live["live_now"] > 0.9 and big_live["gta_focus"] == 1.0 and quiet["gta_focus"] == 0.0
    assert discovery.score_creator([], [], w)["total"] == 0


def _upload(views, hours, game="gta6", language="en", followers_likes=0.05):
    return {"kind": "upload", "game": game, "language": language, "views": views,
            "likes": int(views * followers_likes), "comments": int(views * 0.005), "published_at": NOW - timedelta(hours=hours)}


def test_score_fixes_from_first_real_run():
    """The first real discovery run (2026-11-11) showed three problems; these pin the fixes."""
    w = CFG["score_weights"]
    # 1. viral no longer saturates: faster growth relative to followers still ranks higher
    slow = discovery.score_creator([{"followers": 1_000_000}], [_upload(150_000, 24)], w)     # 15%/day
    fast = discovery.score_creator([{"followers": 1_000_000}], [_upload(1_500_000, 24)], w)   # 150%/day
    assert 0.4 < slow["viral"] < 0.7 and fast["viral"] == 1.0
    # 2. a big non-GTA channel with one GTA 6 video ranks below a smaller GTA-focused one
    tech = discovery.score_creator([{"followers": 4_000_000}],
                                   [_upload(900_000, 20)] + [_upload(800_000, 30 + i, game="other") for i in range(8)], w)
    gta = discovery.score_creator([{"followers": 300_000}], [_upload(250_000, 20 + i) for i in range(6)], w)
    assert gta["gta_focus"] == 1.0 and tech["gta_focus"] < 0.2 and gta["total"] > tech["total"]
    # 3. off-target languages are down-weighted; Brazilian Portuguese is a target language
    es = discovery.score_creator([{"followers": 300_000}], [_upload(250_000, 20 + i, language="es-419") for i in range(6)], w)
    br = discovery.score_creator([{"followers": 300_000}], [_upload(250_000, 20 + i, language="pt-BR") for i in range(6)], w)
    assert es["detail"]["dominant_language"] == "es" and es["detail"]["language_fit"] == 0.25
    assert br["detail"]["language_fit"] == 1.0 and abs(es["total"] - br["total"] * 0.25) < 0.05


# ------------------------------------------------------------------ clients

def test_youtube_client_quota_and_params():
    spent = []
    yt = YouTubeData("test-key", spend=spent.append, session=FakeYouTube())
    yt.search_videos("GTA 6", event_type="live", region="BR", language="pt")
    q = yt.http.calls[-1][1]
    assert q["eventType"] == "live" and q["regionCode"] == "BR" and q["relevanceLanguage"] == "pt" and q["type"] == "video"
    yt.videos(["vidBr1"])
    yt.channel_by_handle("@IShowSpeed")
    assert spent == [COST["search"], 1, 1] == [100, 1, 1]
    with pytest.raises(SourceError):
        YouTubeData("")


def test_youtube_quota_budget_stops_calls(conn):
    budget = discovery.QuotaBudget(conn, "youtube_data", 150)
    yt = YouTubeData("test-key", spend=budget, session=FakeYouTube())
    yt.search_videos("GTA 6")
    with pytest.raises(QuotaExceeded):
        yt.search_videos("GTA 6")                       # 100 + 100 > 150: refused before calling
    assert len(yt.http.calls) == 1 and budget.used() == 100

    class Exhausted(FakeYouTube):
        def get(self, url, params=None, **kw):
            return FakeResponse(status=403, body={"error": {"errors": [{"reason": "quotaExceeded"}]}})
    with pytest.raises(QuotaExceeded):
        YouTubeData("test-key", session=Exhausted()).videos(["x"])


def test_twitch_client_token_reuse_and_soft_failures():
    fake = FakeTwitch()
    tw = Twitch("cid", "secret", session=fake)
    assert tw.game_id("Grand Theft Auto V") == "32982" and tw.game_id("Grand Theft Auto VI") is None
    assert tw.followers_total("100") is None             # app tokens may not see follower totals
    assert tw.schedule("300") == []                       # 404 = no schedule
    assert len(tw.schedule("100")) == 1 and fake.tokens == 1
    with pytest.raises(SourceError):
        Twitch("", "")


# ------------------------------------------------------------------ the full chain against Postgres

def clients(conn):
    yt = YouTubeData("test-key", spend=discovery.QuotaBudget(conn, "youtube_data", 8000), session=FakeYouTube())
    return yt, Twitch("cid", "secret", session=FakeTwitch())


def test_discover_monitor_rank_and_permissions(conn):
    yt, tw = clients(conn)
    cfg = {**CFG, "seeds": [s for s in CFG["seeds"] if s["display_name"] in ("IShowSpeed", "Kai Cenat")]}
    d = discovery.discover(conn, cfg, yt, tw)
    assert not d["errors"], d["errors"]
    assert set(d["seeds_resolved"]) == {"IShowSpeed", "Kai Cenat"}
    assert "GTA Brasil Clips" in d["new_channels"] and "No Reupload Gaming" in d["new_channels"]
    assert "RioGamer" in d["new_channels"]                # Twitch, found live in the GTA category (PT)
    assert "Tiny GTA Fan" not in d["new_channels"] and "smallfry" not in d["new_channels"]
    assert d["skipped_small"] >= 2
    speed = conn.execute("""SELECT cc.* FROM creator_channels cc JOIN creators c ON c.id = cc.creator_id
                            WHERE c.display_name = 'IShowSpeed'""").fetchone()
    assert speed["followers"] == 40_000_000 and speed["discovered_via"] == "seed"
    br = conn.execute("SELECT * FROM creator_channels WHERE handle = '@gtabrasil'").fetchone()
    assert br["language"] == "pt-BR"

    m = discovery.monitor(conn, cfg, yt, tw)
    assert not m["errors"], m["errors"]
    kinds = {r["external_id"]: (r["kind"], r["game"]) for r in conn.execute("SELECT * FROM creator_content").fetchall()}
    assert kinds["vidSpeedLive"] == ("live", "gta6") and kinds["vidSpeedUp"] == ("scheduled_live", "gta6")
    assert kinds["vidSpeedOld"] == ("upload", "other") and kinds["schedule:seg1"] == ("scheduled_live", "gta6")
    assert kinds["stream:s1"] == ("live", "gta5")
    assert any(x["display_name"] == "IShowSpeed" and x["live_viewers"] == 250000 for x in m["live_now"])
    assert any(x["display_name"] == "Kai Cenat" for x in m["scheduled"])
    assert conn.execute("SELECT count(*) AS n FROM creator_content_snapshots").fetchone()["n"] > len(kinds)

    # Kai Cenat goes offline -> the stream is recorded as ended on the next monitor run
    tw.http.live.pop("100")
    m2 = discovery.monitor(conn, cfg, yt, tw)
    assert m2["ended"] >= 1
    assert conn.execute("SELECT kind FROM creator_content WHERE external_id = 'stream:s1'").fetchone()["kind"] == "past_live"

    perms = {p["creator"]: p for p in discovery.check_permissions(conn)}
    assert perms["GTA Brasil Clips"]["status"] == "stated_allow" and "credit_required" in perms["GTA Brasil Clips"]["conditions"]
    assert perms["No Reupload Gaming"]["status"] == "stated_deny"
    assert perms["Kai Cenat"]["status"] == "stated_deny"          # "no reuploads"
    assert perms["RioGamer"]["status"] == "program_available"
    assert perms["IShowSpeed"]["status"] == "needs_review"        # nothing stated -> a person must check
    assert perms["GTA Brasil Clips"]["evidence"][0]["snippet"]
    # nothing is ever authorized automatically
    assert conn.execute("SELECT count(*) AS n FROM creators WHERE permission_status = 'authorized'").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM rights_ledger").fetchone()["n"] == 0

    ranking = {r["creator"]: r for r in discovery.rank(conn, cfg)}
    order = list(ranking)
    assert all(0 <= r["total"] <= 100 for r in ranking.values())
    assert set(order[:2]) == {"IShowSpeed", "GTA Brasil Clips"}    # biggest live audience / fastest-growing upload
    assert ranking["IShowSpeed"]["live_now"] > 0.9 and ranking["IShowSpeed"]["audience"] == max(r["audience"] for r in ranking.values())
    assert ranking["GTA Brasil Clips"]["viral"] == 1.0              # 600k views in 10 h on a 900k channel
    assert ranking["No Reupload Gaming"]["total"] < ranking["GTA Brasil Clips"]["total"]
    report = {r["display_name"]: r for r in discovery.creator_report(conn)}
    assert list(report)[:2] == order[:2]
    assert report["IShowSpeed"]["live_now_count"] == 1 and report["IShowSpeed"]["permission_status"] == "needs_review"

    # re-running discovery does not duplicate creators or channels
    before = conn.execute("SELECT count(*) AS n FROM creator_channels").fetchone()["n"]
    discovery.discover(conn, cfg, yt, tw)
    assert conn.execute("SELECT count(*) AS n FROM creator_channels").fetchone()["n"] == before
    assert conn.execute("SELECT count(*) AS n FROM creators").fetchone()["n"] == before


def test_review_is_the_only_way_to_authorize(conn, settings, other_footage):
    yt, tw = clients(conn)
    discovery.discover(conn, {**CFG, "seeds": []}, yt, tw)
    discovery.check_permissions(conn)
    cid = conn.execute("SELECT id FROM creators WHERE display_name = 'GTA Brasil Clips'").fetchone()["id"]

    with pytest.raises(ValueError, match="evidence-url"):
        discovery.review_creator(conn, cid, decision="authorize", by="owner")
    with pytest.raises(ValueError, match="edited clips"):
        discovery.review_creator(conn, cid, decision="authorize", by="owner", evidence_url="https://example.invalid/p.pdf")
    with pytest.raises(ValueError, match="reviewer"):
        discovery.review_creator(conn, cid, decision="deny", by="")

    res = discovery.review_creator(conn, cid, decision="authorize", by="owner", evidence_url="https://example.invalid/p.pdf",
                                   edits_allowed=True, monetisation_allowed=True, credit_text="Clip: GTA Brasil Clips")
    row = conn.execute("SELECT * FROM rights_ledger WHERE id = %s", (res["rights_id"],)).fetchone()
    assert row["licence_type"] == "creator_permission" and row["verified_by"] == "owner" and row["verified_at"]
    assert row["document_url"] == "https://example.invalid/p.pdf" and row["derivative_edits_allowed"]

    # footage that creator supplies can now pass the rights gate as a creator upload
    r = ingest.ingest_file(conn, settings, other_footage, origin="creator_upload", rights_id=res["rights_id"])
    assert r["status"] == "cleared", r["reasons"]

    # an automatic re-scan never overrides a person's decision
    discovery.check_permissions(conn)
    assert conn.execute("SELECT permission_status FROM creators WHERE id = %s", (cid,)).fetchone()["permission_status"] == "authorized"
    deny_id = conn.execute("SELECT id FROM creators WHERE display_name = 'No Reupload Gaming'").fetchone()["id"]
    assert discovery.review_creator(conn, deny_id, decision="deny", by="owner")["status"] == "denied"
    discovery.check_permissions(conn)
    assert conn.execute("SELECT permission_status FROM creators WHERE id = %s", (deny_id,)).fetchone()["permission_status"] == "denied"


def test_cli_without_keys_skips_cleanly(settings, conn, monkeypatch, capsys):
    from vicepulse import cli
    for k in ("YOUTUBE_DATA_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    def run(*args):
        assert cli.main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    out = run("discovery-run")
    assert out["discover"] == {"skipped": True} and any("YOUTUBE_DATA_API_KEY" in n for n in out["notes"])
    assert run("creators")["creators"] == []
    assert cli.main(["creator-review", "--creator", "999", "--decision", "deny", "--by", "x"]) == 1
