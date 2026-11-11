"""Strict EN / PT-BR targeting: language detection, exclusion without deletion, prioritisation."""

from __future__ import annotations

from datetime import timedelta

import pytest

from test_discovery import CFG, NOW, FakeTwitch, FakeYouTube, yt_channel, yt_video
from vicepulse import discovery, language
from vicepulse.sources.twitch import Twitch
from vicepulse.sources.youtube_data import YouTubeData


@pytest.mark.parametrize("text, lang", [
    ("GTA 6: TODO lo que sabemos del nuevo tráiler y la fecha", "es"),
    ("GTA 6 en vivo con todos ustedes", "es"),
    ("DaniRep | +6 Vídeos Diarios De GTA 5 Online!", "es"),
    ("GTA 6: tudo que sabemos do novo trailer, não perca!", "pt"),
    ("reagindo ao trailer do GTA 6 com vocês", "pt"),
    ("GTA 6 ao vivo", "pt"),
    ("I tried GTA 6 for the first time and it was the best", "en"),
    ("Обзор GTA 6 трейлер", "ru"),
    ("GTA 6 LIVE", None),                      # loanwords only: not enough to decide
    ("", None),
])
def test_detect_text(text, lang):
    assert language.detect_text(text)[0] == lang


def test_classify_channel():
    es = language.classify_channel(country="MX", items=[{"language": "es-419", "text": "GTA 6 todo lo que sabemos"}] * 4)
    assert es["language"] == "es" and es["confirmed"] and language.off_target(es)
    br = language.classify_channel(country="BR", items=[{"language": None, "text": "GTA 6 tudo que sabemos do novo trailer"}] * 3)
    assert br["language"] == "pt" and not language.off_target(br)
    thin = language.classify_channel(items=[{"language": "es", "text": "GTA 6 LIVE"}])
    assert thin["language"] is None and not language.off_target(thin)          # too little evidence: keep, unknown
    mixed = language.classify_channel(items=[{"language": "es"}] * 2 + [{"language": "en"}] * 2)
    assert not language.off_target(mixed)                                     # mixed: not confirmed
    seed = language.classify_channel(declared="en", items=[{"language": "es"}] * 2)
    assert seed["language"] == "en"


class FakeYouTubeWithSpanish(FakeYouTube):
    def __init__(self):
        super().__init__()
        self.channels["UCspanish000000000000000"] = yt_channel("UCspanish000000000000000", "GTA en Español", 3_000_000,
                                                               handle="@gtaespanol", country="MX",
                                                               desc="Videos diarios de GTA en español")
        for i in range(4):
            v = yt_video(f"vidEs{i}", "UCspanish000000000000000", f"GTA 6: todo lo que sabemos del nuevo tráiler {i}",
                         views=900_000, hours_ago=8 + i)
            v["snippet"]["defaultAudioLanguage"] = "es-419"
            self.videos[v["id"]] = v
        self.uploads["UUspanish000000000000000"] = [f"vidEs{i}" for i in range(4)]

    def get(self, url, params=None, **kw):
        r = super().get(url, params, **kw)
        if url.endswith("/search") and not params.get("eventType"):
            r._body["items"] += [{"id": {"videoId": f"vidEs{i}"}} for i in range(4)]
        return r


def make_clients(conn, yt_session):
    return (YouTubeData("test-key", spend=discovery.QuotaBudget(conn, "youtube_data", 8000), session=yt_session),
            Twitch("cid", "secret", session=FakeTwitch()))


def test_discovery_skips_confirmed_off_target_channels(conn):
    yt, tw = make_clients(conn, FakeYouTubeWithSpanish())
    d = discovery.discover(conn, {**CFG, "seeds": []}, yt, tw)
    assert any("GTA en Español" in s and "(es)" in s for s in d["skipped_language"])
    assert "GTA en Español" not in d["new_channels"] and "GTA Brasil Clips" in d["new_channels"]
    assert not conn.execute("SELECT 1 FROM creator_channels WHERE handle = '@gtaespanol'").fetchone()


def test_existing_off_target_channels_stop_being_monitored_but_are_kept(conn):
    fake = FakeYouTubeWithSpanish()
    yt, tw = make_clients(conn, fake)
    discovery.discover(conn, {**CFG, "seeds": [s for s in CFG["seeds"] if s["display_name"] == "IShowSpeed"]}, yt, tw)
    # a Spanish channel that was stored before the language rules existed
    ch = discovery.upsert_channel(conn, platform="youtube", ref="UCspanish000000000000000", name="GTA en Español",
                                  discovered_via="youtube_search", country="MX", handle="@gtaespanol",
                                  uploads_playlist="UUspanish000000000000000", followers=3_000_000,
                                  url="https://www.youtube.com/channel/UCspanish000000000000000")
    for v in fake.videos.values():
        if v["snippet"]["channelId"] == "UCspanish000000000000000":
            discovery.upsert_content(conn, ch["id"], "youtube", discovery.yt_video_item(v, CFG))
    # an unknown-language channel whose titles are Portuguese, and a Spanish-speaking *seed*
    unk = discovery.upsert_channel(conn, platform="youtube", ref="UCunknown000000000000000", name="Mystery GTA",
                                   discovered_via="youtube_search", followers=500_000)
    for i in range(3):
        discovery.upsert_content(conn, unk["id"], "youtube", {"external_id": f"unk{i}", "kind": "upload", "game": "gta6",
                                 "title": f"GTA 6 ao vivo com vocês, tudo que sabemos {i}", "views": 50_000,
                                 "published_at": NOW - timedelta(hours=5)})
    seed = discovery.upsert_channel(conn, platform="youtube", ref="UCseedes0000000000000000", name="Seed Hispano",
                                    discovered_via="seed", seed=True, country="ES", language="es")
    for i in range(4):
        discovery.upsert_content(conn, seed["id"], "youtube", {"external_id": f"seed{i}", "kind": "upload", "game": "gta6",
                                 "language": "es", "title": f"GTA 6 todo lo que sabemos del tráiler {i}", "views": 10_000,
                                 "published_at": NOW - timedelta(hours=6)})
    conn.commit()
    accounts_before = conn.execute("SELECT * FROM platform_accounts ORDER BY id").fetchall()
    content_before = conn.execute("SELECT count(*) AS n FROM creator_content").fetchone()["n"]

    res = discovery.classify_languages(conn, CFG)
    assert [e["channel"] for e in res["excluded"]] == ["GTA en Español"]
    es = conn.execute("SELECT * FROM creator_channels WHERE handle = '@gtaespanol'").fetchone()
    assert es["tracked"] is False and es["detected_language"] == "es" and es["excluded_reason"].startswith("language:es")
    # records are kept
    assert conn.execute("SELECT count(*) AS n FROM creator_content").fetchone()["n"] == content_before
    assert conn.execute("SELECT count(*) AS n FROM creator_content WHERE channel_id = %s", (es["id"],)).fetchone()["n"] == 4
    # unknown channel identified from its Portuguese titles; Spanish seed is kept because the owner chose it
    assert conn.execute("SELECT detected_language, tracked FROM creator_channels WHERE id = %s", (unk["id"],)).fetchone() == \
        {"detected_language": "pt", "tracked": True}
    assert conn.execute("SELECT tracked FROM creator_channels WHERE id = %s", (seed["id"],)).fetchone()["tracked"] is True

    # monitoring no longer polls the excluded channel
    fake.calls.clear()
    discovery.monitor(conn, CFG, yt, tw)
    assert not any(c[0] == "playlistItems" and c[1].get("playlistId") == "UUspanish000000000000000" for c in fake.calls)

    # excluded channels leave the ranking, the creator list and the opportunities; still listable on request
    ranked = [r["creator"] for r in discovery.rank(conn, CFG)]
    assert "GTA en Español" not in ranked and "Mystery GTA" in ranked
    assert "GTA en Español" not in [r["display_name"] for r in discovery.creator_report(conn)]
    every = {r["display_name"]: r for r in discovery.creator_report(conn, include_excluded=True)}
    assert every["GTA en Español"]["excluded"] is True and "language:es" in every["GTA en Español"]["excluded_reason"]
    assert all(o["creator"] != "GTA en Español" for o in discovery.opportunities(conn, per_creator=10))

    # publishing accounts untouched
    assert conn.execute("SELECT * FROM platform_accounts ORDER BY id").fetchall() == accounts_before
    assert len(accounts_before) == 6 and not any(a["enabled"] for a in accounts_before)


def test_confirmed_target_language_outranks_unknown():
    w = CFG["score_weights"]
    items = [{"kind": "upload", "game": "gta6", "views": 200_000, "likes": 8_000, "comments": 800,
              "published_at": NOW - timedelta(hours=10)}]
    known = discovery.score_creator([{"followers": 500_000}], items, w, known_language="pt")
    unknown = discovery.score_creator([{"followers": 500_000}], items, w)
    other = discovery.score_creator([{"followers": 500_000}], items, w, known_language="es")
    assert known["total"] > unknown["total"] > other["total"]
    assert unknown["detail"]["language_fit"] == 0.85 and known["detail"]["language_fit"] == 1.0


def test_occasional_spanish_does_not_exclude_en_or_pt_channels():
    """Owner rule (2026-11-11): occasional Spanish in English / Portuguese channels is fine; exclude only clear cases."""
    en_items = [{"language": "en", "text": "I tried GTA 6 for the first time and it was the best"}] * 6 + \
               [{"language": None, "text": "GTA 6 todo lo que sabemos del nuevo tráiler"}] * 2
    en = language.classify_channel(country="US", items=en_items)
    assert en["language"] == "en" and not language.off_target(en)
    pt_items = [{"language": None, "text": "GTA 6 ao vivo com vocês, tudo que sabemos"}] * 5 + \
               [{"language": "es", "text": "GTA 6 en vivo con todos ustedes"}] * 2
    pt = language.classify_channel(country="BR", items=pt_items)
    assert pt["language"] == "pt" and not language.off_target(pt)
    # even half-and-half is not "clearly off-target": kept as unknown / target
    half = language.classify_channel(items=[{"language": "en"}] * 3 + [{"language": "es"}] * 3)
    assert not language.off_target(half)
