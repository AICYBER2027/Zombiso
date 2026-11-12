"""Brazilian Portuguese searches, the YouTube quota reserve, and the sources-check command."""

from __future__ import annotations

import json

from test_discovery import CFG, FakeTwitch, FakeYouTube
from vicepulse import discovery
from vicepulse.config import PACKAGE_ROOT
from vicepulse.sources.twitch import Twitch
from vicepulse.sources.youtube_data import YouTubeData

PT_QUERIES = {"GTA 6 gameplay português", "GTA RP ao vivo", "GTA 6 reação", "GTA 6 Brasil", "GTA 6 livestream brasileiro"}


def test_brazilian_searches_are_configured_for_brazil_and_portuguese():
    by_q = {s["q"]: s for s in CFG["youtube_searches"]}
    assert PT_QUERIES <= set(by_q)
    for q in PT_QUERIES:
        assert by_q[q]["regionCode"] == "BR" and by_q[q]["relevanceLanguage"] == "pt"
    assert by_q["GTA RP ao vivo"]["eventType"] == "live"
    assert set(CFG["target_languages"]) == {"en", "pt"}


def test_daily_quota_plan_fits_the_budget():
    searches_per_day = len(CFG["youtube_searches"]) * 4             # workflow 08 discovers every 6 hours
    monitor_per_day = 72 * 40                                       # every 20 min, ~40 units for ~35 channels
    assert searches_per_day * 100 + monitor_per_day <= CFG["youtube_daily_quota_budget"] <= 10_000
    assert CFG["monitor_quota_reserve"] >= monitor_per_day


def test_discovery_keeps_a_reserve_for_monitoring(conn):
    fake = FakeYouTube()
    budget = discovery.QuotaBudget(conn, "youtube_data", 3_500)
    yt = YouTubeData("test-key", spend=budget, session=fake)
    cfg = {**CFG, "seeds": [], "monitor_quota_reserve": 3_000}
    out = discovery.discover(conn, cfg, yt, None)
    assert len(out["searches_run"]) == 5 and len(out["searches_skipped"]) == 5     # 500 units allowed for searches
    assert sum(1 for c in fake.calls if c[0] == "search") == 5
    assert budget.used() <= 3_500 - 3_000 + 10                                      # searches + a few 1-unit calls
    # monitoring still has its reserve
    m = discovery.monitor(conn, cfg, yt, None)
    assert not m["errors"]


def test_sources_check_reports_without_secrets(conn, monkeypatch):
    yt = YouTubeData("test-key", spend=discovery.QuotaBudget(conn, "youtube_data", 8000), session=FakeYouTube())
    tw = Twitch("cid", "very-secret", session=FakeTwitch())
    monkeypatch.setattr(discovery, "make_clients", lambda conn, cfg: (yt, tw, []))
    out = discovery.sources_check(conn, CFG)
    assert out["youtube"]["ok"] and out["youtube"]["test"] == "found IShowSpeed" and out["youtube"]["quota_used_today"] == 1
    assert out["twitch"]["ok"] and "id 32982" in out["twitch"]["categories"]["gta5"]
    assert "not on Twitch yet" in out["twitch"]["categories"]["gta6"]
    text = json.dumps(out)
    assert "test-key" not in text and "very-secret" not in text and "app-token" not in text

    monkeypatch.setattr(discovery, "make_clients", lambda conn, cfg: (None, None, ["YouTube skipped", "Twitch skipped"]))
    out = discovery.sources_check(conn, CFG)
    assert out["youtube"] == {"configured": False} and out["twitch"] == {"configured": False}


def test_gitignore_covers_local_outputs():
    lines = (PACKAGE_ROOT / ".gitignore").read_text().split()
    assert {".env", "discovery-run.json", "e2e-output/", "data/"} <= set(lines)
