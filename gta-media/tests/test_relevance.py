"""Fixes from the first real Twitch + EN/PT-BR run (2026-11-12)."""

from __future__ import annotations

from datetime import timedelta

from test_discovery import CFG, NOW, FakeTwitch, FakeYouTube, yt_channel, yt_video
from vicepulse import discovery
from vicepulse.sources.twitch import Twitch
from vicepulse.sources.youtube_data import YouTubeData


class NoisyYouTube(FakeYouTube):
    """Search results that also contain a cartoon countdown stream and the publisher's channel."""

    def __init__(self):
        super().__init__()
        self.channels["UCcartoon000000000000000"] = yt_channel("UCcartoon000000000000000", "Cartoon Live", 5_000_000,
                                                               handle="@cartoonlive")
        self.videos["vidCartoon"] = yt_video("vidCartoon", "UCcartoon000000000000000", "Christmas countdown live 24/7",
                                             views=0, live={"actualStartTime": "2026-11-12T00:00:00Z",
                                                            "concurrentViewers": "2400"})
        self.channels["UCrockstar00000000000000"] = yt_channel("UCrockstar00000000000000", "Rockstar Games", 20_000_000,
                                                               handle="@rockstargames")
        self.videos["vidRockstar"] = yt_video("vidRockstar", "UCrockstar00000000000000", "Grand Theft Auto VI Trailer 3",
                                              views=9_000_000, hours_ago=20)

    def get(self, url, params=None, **kw):
        r = super().get(url, params, **kw)
        if url.endswith("/search"):
            extra = ["vidCartoon"] if params.get("eventType") == "live" else ["vidRockstar"]
            r._body["items"] += [{"id": {"videoId": v}} for v in extra]
        return r


def clients(conn, fake_yt=None, fake_tw=None):
    return (YouTubeData("test-key", spend=discovery.QuotaBudget(conn, "youtube_data", 8000), session=fake_yt or NoisyYouTube()),
            Twitch("cid", "secret", session=fake_tw or FakeTwitch()))


def test_youtube_cannot_use_up_twitchs_share_of_new_creators(conn):
    yt, tw = clients(conn)
    out = discovery.discover(conn, {**CFG, "seeds": [], "max_new_creators_per_run": 1}, yt, tw)
    assert out["added"] == {"youtube": 1, "twitch": 1}
    twitch = conn.execute("SELECT count(*) AS n FROM creator_channels WHERE platform = 'twitch'").fetchone()["n"]
    assert twitch == 1                                             # Twitch still gets its own share


def test_non_gta_and_blocked_channels_are_not_added(conn):
    yt, tw = clients(conn)
    out = discovery.discover(conn, {**CFG, "seeds": []}, yt, tw)
    assert "Cartoon Live" in out["skipped_not_gta"] and "Cartoon Live" not in out["new_channels"]
    assert "Rockstar Games" in out["skipped_blocked"] and "Rockstar Games" not in out["new_channels"]
    assert "GTA Brasil Clips" in out["new_channels"]


def test_existing_blocked_and_non_gta_channels_stop_being_monitored_records_kept(conn):
    yt, tw = clients(conn, fake_yt=FakeYouTube())
    discovery.discover(conn, {**CFG, "seeds": [s for s in CFG["seeds"] if s["display_name"] == "IShowSpeed"]}, yt, tw)
    rock = discovery.upsert_channel(conn, platform="youtube", ref="UCrockstar00000000000000", name="Rockstar Games",
                                    discovered_via="youtube_search", handle="@rockstargames", followers=20_000_000)
    music = discovery.upsert_channel(conn, platform="youtube", ref="UCmusic00000000000000000", name="Music Media",
                                     discovered_via="youtube_search", handle="@musicmedia", followers=900_000)
    speed = conn.execute("""SELECT cc.id FROM creator_channels cc JOIN creators cr ON cr.id = cc.creator_id
                            WHERE cr.display_name = 'IShowSpeed'""").fetchone()["id"]
    for i in range(6):
        discovery.upsert_content(conn, music["id"], "youtube", {"external_id": f"m{i}", "kind": "upload", "game": "other",
                                 "title": f"Lofi beats {i}", "published_at": NOW - timedelta(hours=i + 1)})
        discovery.upsert_content(conn, speed, "youtube", {"external_id": f"s{i}", "kind": "upload", "game": "other",
                                 "title": f"Speed plays FIFA {i}", "published_at": NOW - timedelta(hours=i + 1)})
    conn.commit()
    before = conn.execute("SELECT count(*) AS n FROM creator_content").fetchone()["n"]
    pruned = discovery.prune_untargeted(conn, CFG)
    assert pruned == {"blocked": ["Rockstar Games"], "not_gta": ["Music Media"]}
    rows = {r["display_name"]: r for r in conn.execute(
        "SELECT cc.*, cr.display_name FROM creator_channels cc JOIN creators cr ON cr.id = cc.creator_id").fetchall()}
    assert rows["Rockstar Games"]["tracked"] is False and rows["Rockstar Games"]["excluded_reason"].startswith("blocked")
    assert rows["Music Media"]["tracked"] is False and rows["Music Media"]["excluded_reason"].startswith("not GTA")
    assert conn.execute("SELECT tracked FROM creator_channels WHERE id = %s", (speed,)).fetchone()["tracked"] is True  # seed
    assert conn.execute("SELECT count(*) AS n FROM creator_content").fetchone()["n"] == before                 # records kept
    names = [r["display_name"] for r in discovery.creator_report(conn)]
    assert "Rockstar Games" not in names and "Music Media" not in names and "IShowSpeed" in names


def test_live_now_lists_only_gta_streams(conn):
    tw_fake = FakeTwitch()
    tw_fake.live["100"]["game_name"], tw_fake.live["100"]["title"] = "World of Warcraft", "DROPS WoW rogue"
    yt, tw = clients(conn, fake_yt=FakeYouTube(), fake_tw=tw_fake)
    cfg = {**CFG, "seeds": [s for s in CFG["seeds"] if s["display_name"] in ("IShowSpeed", "Kai Cenat")]}
    discovery.discover(conn, cfg, yt, tw)
    m = discovery.monitor(conn, cfg, yt, tw)
    assert m["live_now"] and all(x["game"] in ("gta6", "gta5") for x in m["live_now"])
    assert not any(x["display_name"] == "Kai Cenat" for x in m["live_now"])        # streaming WoW, not GTA


def test_twitch_only_discovery_spends_no_youtube_quota(conn, settings, monkeypatch, capsys):
    import json as _json
    from vicepulse import cli
    fake_yt = FakeYouTube()
    yt, tw = clients(conn, fake_yt=fake_yt)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(discovery, "make_clients", lambda conn, cfg: (yt, tw, []))
    assert cli.main(["discover", "--only", "twitch"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["added"]["youtube"] == 0 and out["added"]["twitch"] >= 1 and out["searches_run"] == []
    assert cli.main(["monitor", "--only", "twitch"]) == 0
    capsys.readouterr()
    assert fake_yt.calls == []                                        # not a single YouTube request
    live = conn.execute("""SELECT cc.language, ct.game FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                           WHERE ct.platform = 'twitch' AND ct.kind = 'live'""").fetchall()
    assert {r["language"] for r in live} <= {"en", "pt-BR"} and live
