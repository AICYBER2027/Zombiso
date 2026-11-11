"""Priority 1: discover GTA creators, monitor their uploads and streams, rank them, track permissions.

Everything here is read-only against public, official APIs (YouTube Data API v3, Twitch Helix). It never
downloads footage and never grants rights: a creator becomes usable only after a person records
documented authorization with ``review_creator`` (CLI: ``vicepulse creator-review``).
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from statistics import median

from . import creator_policy, db
from .config import PACKAGE_ROOT
from .sources import QuotaExceeded, SourceError, iso_duration_s, twitch_duration_s
from .sources.twitch import Twitch
from .sources.youtube_data import YouTubeData

LIVE_FRESH = timedelta(minutes=30)       # a 'live' row not refreshed for this long is treated as ended


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("VICEPULSE_DISCOVERY_FILE") or str(PACKAGE_ROOT / "config" / "discovery.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def detect_game(text: str, cfg: dict) -> str:
    text = (text or "").lower()
    for game in ("gta6", "gta5"):                       # gta6 first: "gta 6" must not count as gta5
        if any(re.search(p, text) for p in cfg["games"][game]["patterns"]):
            return game
    return "other"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- quota

class QuotaBudget:
    """Daily YouTube unit budget stored in api_usage; refuses a call that would go over it."""

    def __init__(self, conn, api: str, limit: int):
        self.conn, self.api, self.limit = conn, api, limit

    def used(self) -> int:
        row = self.conn.execute("SELECT units FROM api_usage WHERE day = CURRENT_DATE AND api = %s", (self.api,)).fetchone()
        return row["units"] if row else 0

    def __call__(self, units: int) -> None:
        if self.used() + units > self.limit:
            raise QuotaExceeded(f"{self.api} daily budget of {self.limit} units reached")
        self.conn.execute("""INSERT INTO api_usage (day, api, units) VALUES (CURRENT_DATE, %s, %s)
                             ON CONFLICT (day, api) DO UPDATE SET units = api_usage.units + EXCLUDED.units""",
                          (self.api, units))
        self.conn.commit()


def make_clients(conn, cfg: dict) -> tuple[YouTubeData | None, Twitch | None, list[str]]:
    """Real clients from environment variables. Missing keys are reported, not fatal."""
    notes, yt, tw = [], None, None
    key = os.environ.get("YOUTUBE_DATA_API_KEY", "")
    if key:
        yt = YouTubeData(key, spend=QuotaBudget(conn, "youtube_data", int(os.environ.get(
            "YOUTUBE_DAILY_QUOTA_BUDGET", cfg.get("youtube_daily_quota_budget", 8000)))))
    else:
        notes.append("YouTube skipped: YOUTUBE_DATA_API_KEY is not set")
    cid, secret = os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", "")
    if cid and secret:
        tw = Twitch(cid, secret)
    else:
        notes.append("Twitch skipped: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set")
    return yt, tw, notes


# ---------------------------------------------------------------- storage helpers

def _creator_for(conn, name: str, language: str | None, seed: bool) -> int:
    row = conn.execute("SELECT id FROM creators WHERE lower(display_name) = lower(%s)", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO creators (display_name, primary_language, seed) VALUES (%s, %s, %s) RETURNING id",
                        (name, language, seed)).fetchone()["id"]


def upsert_channel(conn, *, platform: str, ref: str, name: str, discovered_via: str, language: str | None = None,
                   seed: bool = False, **fields) -> dict:
    existing = conn.execute("SELECT * FROM creator_channels WHERE platform = %s AND channel_ref = %s",
                            (platform, ref)).fetchone()
    cols = {k: v for k, v in fields.items() if v is not None}
    if existing:
        if cols:
            sets = ", ".join(f"{k} = %s" for k in cols)
            conn.execute(f"UPDATE creator_channels SET {sets} WHERE id = %s", (*cols.values(), existing["id"]))
        return {**existing, **cols, "new": False}
    creator_id = _creator_for(conn, name, language, seed)
    cols.update(creator_id=creator_id, platform=platform, channel_ref=ref, discovered_via=discovered_via,
                language=language)
    row = conn.execute(f"INSERT INTO creator_channels ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                       tuple(cols.values())).fetchone()
    db.log_event(conn, "discovery", "creator_channel", row["id"], "discovered", {"platform": platform, "via": discovered_via})
    return {**row, "new": True}


def upsert_content(conn, channel_id: int, platform: str, item: dict) -> int:
    cols = {k: v for k, v in item.items() if v is not None}
    ext = cols.pop("external_id")
    row = conn.execute("SELECT id FROM creator_content WHERE platform = %s AND external_id = %s", (platform, ext)).fetchone()
    if row:
        sets = ", ".join(f"{k} = %s" for k in cols)
        conn.execute(f"UPDATE creator_content SET {sets}{', ' if sets else ''}last_seen_at = now() WHERE id = %s",
                     (*cols.values(), row["id"]))
        cid = row["id"]
    else:
        cols.update(channel_id=channel_id, platform=platform, external_id=ext)
        cid = conn.execute(f"INSERT INTO creator_content ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                           tuple(cols.values())).fetchone()["id"]
    conn.execute("INSERT INTO creator_content_snapshots (content_id, views, likes, comments, live_viewers) VALUES (%s, %s, %s, %s, %s)",
                 (cid, item.get("views"), item.get("likes"), item.get("comments"), item.get("live_viewers")))
    if item.get("live_viewers") is not None:
        conn.execute("UPDATE creator_content SET peak_viewers = GREATEST(COALESCE(peak_viewers, 0), %s) WHERE id = %s",
                     (item["live_viewers"], cid))
    return cid


# ---------------------------------------------------------------- YouTube mapping

def _yt_channel_fields(ch: dict) -> dict:
    sn, st = ch.get("snippet", {}), ch.get("statistics", {})
    desc = sn.get("description") or ""
    keywords = (ch.get("brandingSettings", {}).get("channel", {}) or {}).get("keywords") or ""
    return {"handle": sn.get("customUrl"), "title": sn.get("title"), "country": sn.get("country"),
            "description": (desc + ("\n" + keywords if keywords else "")).strip() or None,
            "url": f"https://www.youtube.com/channel/{ch['id']}",
            "uploads_playlist": ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
            "followers": None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
            "total_views": _int(st.get("viewCount")), "video_count": _int(st.get("videoCount"))}


def yt_video_item(v: dict, cfg: dict) -> dict:
    sn, st, live = v.get("snippet", {}), v.get("statistics", {}), v.get("liveStreamingDetails") or {}
    if live.get("actualStartTime") and not live.get("actualEndTime"):
        kind = "live"
    elif live.get("scheduledStartTime") and not live.get("actualStartTime"):
        kind = "scheduled_live"
    elif live.get("actualEndTime"):
        kind = "past_live"
    else:
        kind = "upload"
    text = " ".join([sn.get("title") or "", " ".join(sn.get("tags") or []), (sn.get("description") or "")[:500]])
    return {"external_id": v["id"], "kind": kind, "title": sn.get("title"), "description": sn.get("description"),
            "url": f"https://www.youtube.com/watch?v={v['id']}",
            "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"), "game": detect_game(text, cfg),
            "published_at": _ts(sn.get("publishedAt")), "scheduled_start": _ts(live.get("scheduledStartTime")),
            "actual_start": _ts(live.get("actualStartTime")), "actual_end": _ts(live.get("actualEndTime")),
            "duration_s": iso_duration_s(v.get("contentDetails", {}).get("duration")),
            "views": _int(st.get("viewCount")), "likes": _int(st.get("likeCount")),
            "comments": _int(st.get("commentCount")),
            "live_viewers": _int(live.get("concurrentViewers")) if kind == "live" else None}


def _save_yt_channel(conn, ch: dict, *, via: str, name: str | None = None, language: str | None = None,
                     seed: bool = False) -> dict:
    f = _yt_channel_fields(ch)
    return upsert_channel(conn, platform="youtube", ref=ch["id"], name=name or f["title"] or ch["id"],
                          discovered_via=via, language=language, seed=seed, **f)


# ---------------------------------------------------------------- Twitch mapping

def tw_stream_item(s: dict, cfg: dict) -> dict:
    game = detect_game(s.get("game_name", "") + " " + (s.get("title") or ""), cfg)
    return {"external_id": f"stream:{s['id']}", "kind": "live", "title": s.get("title"),
            "url": f"https://www.twitch.tv/{s.get('user_login')}", "language": s.get("language"), "game": game,
            "actual_start": _ts(s.get("started_at")), "live_viewers": _int(s.get("viewer_count"))}


def _save_tw_user(conn, tw: Twitch, u: dict, *, via: str, name: str | None = None, language: str | None = None,
                  seed: bool = False) -> dict:
    return upsert_channel(conn, platform="twitch", ref=u["id"], name=name or u.get("display_name") or u["login"],
                          discovered_via=via, language=language, seed=seed, handle=u.get("login"),
                          title=u.get("display_name"), description=u.get("description") or None,
                          url=f"https://www.twitch.tv/{u.get('login')}", followers=tw.followers_total(u["id"]))


# ---------------------------------------------------------------- discover

def discover(conn, cfg: dict, yt: YouTubeData | None, tw: Twitch | None) -> dict:
    out = {"seeds_resolved": [], "seeds_not_found": [], "new_channels": [], "skipped_small": 0, "errors": []}
    limit = cfg.get("max_new_creators_per_run", 25)
    min_f = cfg.get("min_followers", 0)

    # 1. seeds (only those not resolved yet)
    for seed in cfg.get("seeds", []):
        try:
            if yt and seed.get("youtube_handle"):
                h = seed["youtube_handle"].lstrip("@").lower()
                if not conn.execute("SELECT 1 FROM creator_channels WHERE platform = 'youtube' AND lower(handle) = %s",
                                    ("@" + h,)).fetchone():
                    ch = yt.channel_by_handle(h)
                    if ch:
                        _save_yt_channel(conn, ch, via="seed", name=seed["display_name"], language=seed.get("language"), seed=True)
                        out["seeds_resolved"].append(seed["display_name"])
                    else:
                        out["seeds_not_found"].append(seed["youtube_handle"])
            if tw and seed.get("twitch_login"):
                if not conn.execute("SELECT 1 FROM creator_channels WHERE platform = 'twitch' AND lower(handle) = %s",
                                    (seed["twitch_login"].lower(),)).fetchone():
                    users = tw.users(logins=[seed["twitch_login"]])
                    if users:
                        _save_tw_user(conn, tw, users[0], via="seed", name=seed["display_name"],
                                      language=seed.get("language"), seed=True)
                        out["seeds_resolved"].append(seed["display_name"])
                    else:
                        out["seeds_not_found"].append(seed["twitch_login"])
        except QuotaExceeded as e:
            out["errors"].append(str(e))
            yt = None
        except SourceError as e:
            out["errors"].append(f"seed {seed['display_name']}: {e}")
    conn.commit()

    # 2. YouTube searches: GTA 6 videos, live and upcoming streams, EN and BR
    if yt:
        try:
            video_ids: list[str] = []
            for s in cfg.get("youtube_searches", []):
                after = None if s.get("eventType") else (_now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for it in yt.search_videos(s["q"], event_type=s.get("eventType"), region=s.get("regionCode"),
                                           language=s.get("relevanceLanguage"), published_after=after):
                    vid = it.get("id", {}).get("videoId")
                    if vid and vid not in video_ids:
                        video_ids.append(vid)
            videos = yt.videos(video_ids)
            by_channel: dict[str, list[dict]] = {}
            for v in videos:
                by_channel.setdefault(v["snippet"]["channelId"], []).append(v)
            known = {r["channel_ref"] for r in conn.execute(
                "SELECT channel_ref FROM creator_channels WHERE platform = 'youtube'").fetchall()}
            unknown = [c for c in by_channel if c not in known]
            for ch in yt.channels(unknown):
                subs = _int(ch.get("statistics", {}).get("subscriberCount")) or 0
                if subs < min_f:
                    out["skipped_small"] += 1
                    continue
                if len(out["new_channels"]) >= limit:
                    break
                lang = "pt-BR" if (ch.get("snippet", {}).get("country") == "BR") else None
                row = _save_yt_channel(conn, ch, via="youtube_search", language=lang)
                out["new_channels"].append(row["title"])
                known.add(ch["id"])
            for cref, vs in by_channel.items():
                if cref not in known:
                    continue
                cid = conn.execute("SELECT id FROM creator_channels WHERE platform = 'youtube' AND channel_ref = %s",
                                   (cref,)).fetchone()["id"]
                for v in vs:
                    upsert_content(conn, cid, "youtube", yt_video_item(v, cfg))
            conn.commit()
        except QuotaExceeded as e:
            out["errors"].append(str(e))
        except SourceError as e:
            out["errors"].append(f"youtube search: {e}")

    # 3. Twitch: who is live in the GTA categories right now (EN + PT)
    if tw:
        try:
            for game in ("gta6", "gta5"):
                gid = tw.game_id(cfg["games"][game]["twitch_category"])
                if not gid:
                    continue
                streams = []
                for lang in cfg.get("twitch_languages", ["en"]):
                    streams += tw.streams_for_game(gid, language=lang)
                users = {u["id"]: u for u in tw.users(ids=sorted({s["user_id"] for s in streams})[:100])}
                for s in sorted(streams, key=lambda s: -s.get("viewer_count", 0)):
                    row = conn.execute("SELECT * FROM creator_channels WHERE platform = 'twitch' AND channel_ref = %s",
                                       (s["user_id"],)).fetchone()
                    if row is None:
                        u = users.get(s["user_id"])
                        if not u or len(out["new_channels"]) >= limit:
                            continue
                        followers = tw.followers_total(u["id"])
                        big_enough = (followers >= min_f) if followers is not None else \
                            (s.get("viewer_count", 0) >= cfg.get("min_live_viewers", 500))
                        if not big_enough:
                            out["skipped_small"] += 1
                            continue
                        row = upsert_channel(conn, platform="twitch", ref=u["id"], name=u.get("display_name") or u["login"],
                                             discovered_via=f"twitch_category:{game}",
                                             language="pt-BR" if s.get("language") == "pt" else s.get("language"),
                                             handle=u.get("login"), title=u.get("display_name"),
                                             description=u.get("description") or None,
                                             url=f"https://www.twitch.tv/{u.get('login')}", followers=followers)
                        out["new_channels"].append(row["title"])
                    upsert_content(conn, row["id"], "twitch", tw_stream_item(s, cfg))
            conn.commit()
        except SourceError as e:
            out["errors"].append(f"twitch: {e}")
    return out


# ---------------------------------------------------------------- monitor

def monitor(conn, cfg: dict, yt: YouTubeData | None, tw: Twitch | None, *, uploads_per_channel: int = 10) -> dict:
    out = {"youtube_channels": 0, "twitch_channels": 0, "items": 0, "live_now": [], "scheduled": [], "ended": 0,
           "errors": []}
    if yt:
        chans = conn.execute("SELECT * FROM creator_channels WHERE platform = 'youtube' AND tracked ORDER BY id").fetchall()
        try:
            for ch in yt.channels([c["channel_ref"] for c in chans]):          # refresh subscriber counts
                f = _yt_channel_fields(ch)
                conn.execute("""UPDATE creator_channels SET followers = COALESCE(%s, followers), total_views = %s,
                                video_count = %s, description = %s, uploads_playlist = COALESCE(%s, uploads_playlist)
                                WHERE platform = 'youtube' AND channel_ref = %s""",
                             (f["followers"], f["total_views"], f["video_count"], f["description"], f["uploads_playlist"], ch["id"]))
            ids, owner = [], {}
            for c in chans:
                if not c["uploads_playlist"]:
                    continue
                for vid in yt.latest_uploads(c["uploads_playlist"], uploads_per_channel):
                    ids.append(vid)
                    owner[vid] = c["id"]
                out["youtube_channels"] += 1
            # also refresh anything we last saw live or scheduled
            for r in conn.execute("""SELECT external_id, channel_id FROM creator_content WHERE platform = 'youtube'
                                     AND kind IN ('live', 'scheduled_live')""").fetchall():
                if r["external_id"] not in owner:
                    ids.append(r["external_id"])
                    owner[r["external_id"]] = r["channel_id"]
            for v in yt.videos(ids):
                item = yt_video_item(v, cfg)
                upsert_content(conn, owner[v["id"]], "youtube", item)
                out["items"] += 1
            conn.execute("UPDATE creator_channels SET last_polled_at = now() WHERE platform = 'youtube' AND tracked")
            conn.commit()
        except QuotaExceeded as e:
            out["errors"].append(str(e))
        except SourceError as e:
            out["errors"].append(f"youtube: {e}")
    if tw:
        chans = conn.execute("SELECT * FROM creator_channels WHERE platform = 'twitch' AND tracked ORDER BY id").fetchall()
        try:
            by_ref = {c["channel_ref"]: c for c in chans}
            live_ids = set()
            for s in tw.streams_for_users(list(by_ref)):
                upsert_content(conn, by_ref[s["user_id"]]["id"], "twitch", tw_stream_item(s, cfg))
                live_ids.add(f"stream:{s['id']}")
                out["items"] += 1
            ended = conn.execute("""UPDATE creator_content SET kind = 'past_live', actual_end = now()
                                    WHERE platform = 'twitch' AND kind = 'live' AND NOT (external_id = ANY(%s::text[])) RETURNING id""",
                                 (list(live_ids),)).fetchall()
            out["ended"] += len(ended)
            for c in chans:
                for seg in tw.schedule(c["channel_ref"]):
                    if seg.get("canceled_until"):
                        continue
                    title = seg.get("title") or ""
                    game = detect_game(title + " " + ((seg.get("category") or {}).get("name") or ""), cfg)
                    upsert_content(conn, c["id"], "twitch", {"external_id": f"schedule:{seg['id']}", "kind": "scheduled_live",
                                   "title": title, "url": c["url"], "game": game,
                                   "scheduled_start": _ts(seg.get("start_time"))})
                    out["items"] += 1
                for v in tw.videos(c["channel_ref"]):
                    upsert_content(conn, c["id"], "twitch", {
                        "external_id": f"video:{v['id']}", "kind": "past_live", "title": v.get("title"),
                        "description": v.get("description") or None, "url": v.get("url"), "language": v.get("language"),
                        "game": detect_game(v.get("title") or "", cfg), "published_at": _ts(v.get("published_at")),
                        "duration_s": twitch_duration_s(v.get("duration")), "views": _int(v.get("view_count"))})
                    out["items"] += 1
                out["twitch_channels"] += 1
            conn.execute("UPDATE creator_channels SET last_polled_at = now() WHERE platform = 'twitch' AND tracked")
            conn.commit()
        except SourceError as e:
            out["errors"].append(f"twitch: {e}")
    # YouTube lives that disappeared from the API answers count as ended after LIVE_FRESH
    stale = conn.execute("""UPDATE creator_content SET kind = 'past_live' WHERE platform = 'youtube' AND kind = 'live'
                            AND last_seen_at < now() - %s::interval RETURNING id""", (f"{int(LIVE_FRESH.total_seconds())} seconds",)).fetchall()
    out["ended"] += len(stale)
    conn.commit()
    for r in conn.execute("""SELECT cr.display_name, cc.platform, ct.title, ct.live_viewers, ct.game, ct.url
                             FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                             JOIN creators cr ON cr.id = cc.creator_id
                             WHERE ct.kind = 'live' ORDER BY ct.live_viewers DESC NULLS LAST LIMIT 20""").fetchall():
        out["live_now"].append(dict(r))
    for r in conn.execute("""SELECT cr.display_name, cc.platform, ct.title, ct.scheduled_start, ct.game, ct.url
                             FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                             JOIN creators cr ON cr.id = cc.creator_id
                             WHERE ct.kind = 'scheduled_live' AND ct.scheduled_start > now()
                             ORDER BY ct.scheduled_start LIMIT 20""").fetchall():
        out["scheduled"].append(dict(r))
    return out


# ---------------------------------------------------------------- rank

def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _lang(code: str | None) -> str | None:
    return code.split("-")[0].lower() if code else None


def score_creator(channels: list[dict], content: list[dict], weights: dict, now: datetime | None = None, *,
                  target_languages: tuple[str, ...] = ("en", "pt"), gta_floor: float = 0.25,
                  star_floor: float = 0.9, other_language_factor: float = 0.25) -> dict:
    """Pure scoring; each part is 0..1 and the total is 0..100.

    total = 100 x weighted(audience, engagement, viral, live_now) x GTA factor x language factor
      GTA factor      = floor + (1 - floor) x gta_focus, where the floor rises from gta_floor (small channels)
                        to star_floor (50M+ followers): a mid-size channel with one GTA video ranks low, a star
                        who rarely plays GTA still ranks high because their audience follows them into the game
      language factor = 1 for the target languages or unknown, other_language_factor otherwise
    """
    now = now or _now()
    followers = max([c.get("followers") or 0 for c in channels] or [0])
    audience = _clamp(math.log10(followers + 1) / 8)                   # 100M followers -> 1.0
    recent = [c for c in content if (c.get("published_at") or c.get("actual_start") or now) >= now - timedelta(days=14)]
    rates = [((c.get("likes") or 0) + (c.get("comments") or 0)) / c["views"]
             for c in recent if (c.get("views") or 0) >= 1000 and c.get("kind") in ("upload", "past_live")]
    engagement = _clamp((median(rates) if rates else 0) / 0.08)       # 8% likes+comments per view -> 1.0
    per_day = []
    for c in recent:
        if c.get("kind") != "upload" or not c.get("views") or not c.get("published_at"):
            continue
        hours = (now - c["published_at"]).total_seconds() / 3600
        if 1 <= hours <= 24 * 7:
            per_day.append(c["views"] / hours * 24)
    velocities = [v / max(followers, 1) for v in per_day]                # views per day per follower
    # Relative growth, log scale: 1% of followers per day -> 0.15, 10% -> 0.52, 100% -> 1.0 (a linear scale
    # saturated during the GTA 6 pre-launch hype). Absolute reach, log scale: 10M views per day -> 1.0, so a
    # star whose huge audience makes relative growth look small still counts as viral. Median of the 3 fastest.
    ratio = median(sorted(velocities)[-3:]) if velocities else 0.0
    daily = median(sorted(per_day)[-3:]) if per_day else 0.0
    viral = max(_clamp(math.log10(1 + ratio / 0.01) / math.log10(101)), _clamp(math.log10(daily + 1) / 7))
    weights_game = {"gta6": 1.0, "gta5": 0.5}
    gta_focus = (sum(weights_game.get(c.get("game"), 0) for c in recent) / len(recent)) if recent else 0.0
    live = [c for c in content if c.get("kind") == "live" and (c.get("game") in weights_game)]
    live_now = _clamp(math.log10(max([c.get("live_viewers") or 0 for c in live] or [0]) + 1) / 5)
    langs = [_lang(c.get("language")) for c in recent if c.get("language")]
    dominant = max(set(langs), key=langs.count) if langs else None
    if dominant is None:
        dominant = next((_lang(c.get("language")) for c in channels if c.get("language")), None)
    language_fit = 1.0 if dominant is None or dominant in target_languages else other_language_factor
    base_parts = {"audience": audience, "engagement": engagement, "viral": viral, "live_now": live_now}
    wsum = sum(weights.get(k, 0) for k in base_parts) or 1e-9
    base = sum(weights.get(k, 0) * v for k, v in base_parts.items()) / wsum
    # Audiences follow the creator, not the game: when a 60M-subscriber star plays GTA, that audience watches
    # it. So the bigger the audience, the less GTA focus matters ("star power": 0 at 1M followers, 1 at 50M+).
    star = _clamp((math.log10(followers + 1) - 6) / math.log10(50))
    floor = gta_floor + (star_floor - gta_floor) * star
    gta_factor = floor + (1 - floor) * gta_focus
    total = 100 * base * gta_factor * language_fit
    return {**{k: round(v, 4) for k, v in {**base_parts, "gta_focus": gta_focus}.items()}, "total": round(total, 2),
            "detail": {"followers": followers, "recent_items": len(recent), "engagement_samples": len(rates),
                       "velocity_samples": len(velocities), "velocity_ratio": round(ratio, 4), "views_per_day": round(daily),
                       "dominant_language": dominant, "language_fit": language_fit, "gta_factor": round(gta_factor, 3),
                       "star_power": round(star, 3)}}


def rank(conn, cfg: dict) -> list[dict]:
    weights = cfg.get("score_weights", {})
    out = []
    for cr in conn.execute("SELECT * FROM creators ORDER BY id").fetchall():
        chans = conn.execute("SELECT * FROM creator_channels WHERE creator_id = %s", (cr["id"],)).fetchall()
        content = conn.execute("""SELECT ct.* FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                                  WHERE cc.creator_id = %s AND ct.last_seen_at > now() - interval '21 days'""",
                               (cr["id"],)).fetchall()
        s = score_creator(chans, content, weights,
                          target_languages=tuple(cfg.get("target_languages", ["en", "pt"])),
                          gta_floor=cfg.get("gta_focus_floor", 0.25), star_floor=cfg.get("star_focus_floor", 0.9),
                          other_language_factor=cfg.get("other_language_factor", 0.25))
        conn.execute("""INSERT INTO creator_scores (creator_id, audience, engagement, viral, gta_focus, live_now, total, detail)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                     (cr["id"], s["audience"], s["engagement"], s["viral"], s["gta_focus"], s["live_now"], s["total"],
                      db.Jsonb(s["detail"])))
        out.append({"creator_id": cr["id"], "creator": cr["display_name"], **{k: s[k] for k in ("total", "audience",
                    "engagement", "viral", "gta_focus", "live_now")}, "permission": cr["permission_status"]})
    conn.commit()
    return sorted(out, key=lambda r: -r["total"])


# ---------------------------------------------------------------- permissions

def check_permissions(conn) -> list[dict]:
    out = []
    for cr in conn.execute("SELECT * FROM creators ORDER BY id").fetchall():
        texts = []
        for ch in conn.execute("SELECT * FROM creator_channels WHERE creator_id = %s", (cr["id"],)).fetchall():
            texts.append({"source": f"{ch['platform']} channel description", "url": ch["url"], "text": ch["description"]})
        for ct in conn.execute("""SELECT ct.* FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                                  WHERE cc.creator_id = %s AND ct.description IS NOT NULL
                                  ORDER BY ct.last_seen_at DESC LIMIT 20""", (cr["id"],)).fetchall():
            texts.append({"source": f"{ct['platform']} {ct['kind']} description", "url": ct["url"], "text": ct["description"]})
        res = creator_policy.scan(texts)
        conn.execute("INSERT INTO creator_permission_checks (creator_id, verdict, conditions, evidence) VALUES (%s, %s, %s, %s)",
                     (cr["id"], res.verdict, res.conditions, db.Jsonb(res.evidence)))
        status = cr["permission_status"]
        if status not in creator_policy.HUMAN_STATUSES:
            status = creator_policy.STATUS_FOR_VERDICT[res.verdict]
            summary = (f"automatic check: {res.verdict}"
                       + (f" ({', '.join(res.conditions)})" if res.conditions else "")
                       + f"; {len(res.evidence)} statement(s) found in {len(texts)} text(s). Needs a person to review "
                       "the evidence before any footage can be used.")
            conn.execute("""UPDATE creators SET permission_status = %s, permission_summary = %s,
                            permission_checked_at = now() WHERE id = %s""", (status, summary, cr["id"]))
        out.append({"creator_id": cr["id"], "creator": cr["display_name"], "verdict": res.verdict,
                    "status": status, "conditions": res.conditions, "evidence": res.evidence[:5]})
    conn.commit()
    return out


def review_creator(conn, creator_id: int, *, decision: str, by: str, evidence_url: str | None = None,
                   notes: str | None = None, platforms: list[str] | None = None, languages: list[str] | None = None,
                   monetisation_allowed: bool = False, edits_allowed: bool = False, expires_on: str | None = None,
                   credit_text: str | None = None) -> dict:
    """A person's decision. 'authorize' needs saved evidence and writes a verified rights_ledger row."""
    cr = conn.execute("SELECT * FROM creators WHERE id = %s", (creator_id,)).fetchone()
    if cr is None:
        raise ValueError(f"unknown creator {creator_id}")
    if not by:
        raise ValueError("the reviewer's name is required")
    if decision == "authorize":
        if not (evidence_url and evidence_url.startswith("https://")):
            raise ValueError("authorizing needs --evidence-url: an https link to the saved licence, written permission "
                             "or archived copy of the creator's published clipping policy")
        if not edits_allowed:
            raise ValueError("the evidence must allow edited clips (--edits-allowed); VicePulse never reposts unedited footage")
        rid = conn.execute(
            """INSERT INTO rights_ledger (owner_name, licence_type, platforms, languages, starts_on, expires_on,
                   monetisation_allowed, derivative_edits_allowed, credit_text, document_url, verified_by, verified_at, notes)
               VALUES (%s, 'creator_permission', %s, %s, CURRENT_DATE, %s, %s, TRUE, %s, %s, %s, now(), %s) RETURNING id""",
            (cr["display_name"], platforms or ["youtube", "instagram", "tiktok"], languages or ["*"], expires_on,
             monetisation_allowed, credit_text, evidence_url, by, notes)).fetchone()["id"]
        conn.execute("""UPDATE creators SET permission_status = 'authorized', rights_id = %s, reviewed_by = %s,
                        reviewed_at = now(), review_notes = %s WHERE id = %s""", (rid, by, notes, creator_id))
        db.log_event(conn, "creator_review", "creator", creator_id, "authorized", {"by": by, "rights_id": rid, "evidence": evidence_url})
        conn.commit()
        return {"creator_id": creator_id, "status": "authorized", "rights_id": rid}
    status = {"deny": "denied", "needs_info": "needs_review"}.get(decision)
    if status is None:
        raise ValueError("decision must be authorize, deny or needs_info")
    conn.execute("""UPDATE creators SET permission_status = %s, reviewed_by = %s, reviewed_at = now(), review_notes = %s
                    WHERE id = %s""", (status, by, notes, creator_id))
    db.log_event(conn, "creator_review", "creator", creator_id, status, {"by": by, "notes": notes})
    conn.commit()
    return {"creator_id": creator_id, "status": status}


def score_opportunity(item: dict, followers: int, now: datetime | None = None) -> float:
    """0..100: how much a single GTA livestream or upload is worth clipping right now.

    Reach is what is actually happening (live viewers, or views per day for an upload), plus the creator's
    audience, because a star's GTA moment spreads far beyond the people watching it live.
    """
    now = now or _now()
    if item.get("game") not in ("gta6", "gta5"):
        return 0.0
    if item.get("kind") == "live":
        reach, fresh = (item.get("live_viewers") or 0) * 20, 1.0            # ~20 views later per live viewer
    else:
        start = item.get("published_at") or item.get("actual_start")
        if not start:
            return 0.0
        hours = max((now - start).total_seconds() / 3600, 1.0)
        reach = (item.get("views") or 0) / hours * 24
        fresh = _clamp(1 - (hours - 6) / 66)                                # full for 6 h, gone after 72 h
    reach_part = _clamp(math.log10(reach + 1) / 7)                          # 10M expected views -> 1.0
    audience_part = _clamp(math.log10(followers + 1) / 8)
    game = 1.0 if item.get("game") == "gta6" else 0.7
    return round(100 * (0.6 * reach_part + 0.4 * audience_part) * game * fresh, 2)


def opportunities(conn, limit: int = 30) -> list[dict]:
    """GTA livestreams happening now and fresh GTA uploads, best first, with the creator's permission status."""
    rows = conn.execute(
        """SELECT ct.*, cc.platform AS ch_platform, cc.handle, cc.followers, cr.id AS creator_id, cr.display_name,
                  cr.permission_status, cr.rights_id
           FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id JOIN creators cr ON cr.id = cc.creator_id
           WHERE ct.game IN ('gta6', 'gta5')
             AND (ct.kind = 'live' OR (ct.kind IN ('upload', 'past_live')
                  AND COALESCE(ct.published_at, ct.actual_start) > now() - interval '72 hours'))""").fetchall()
    out = []
    for r in rows:
        score = score_opportunity(r, r["followers"] or 0)
        if score <= 0:
            continue
        out.append({"score": score, "creator": r["display_name"], "creator_id": r["creator_id"],
                    "kind": r["kind"], "game": r["game"], "title": r["title"], "url": r["url"],
                    "live_viewers": r["live_viewers"], "views": r["views"], "followers": r["followers"],
                    "permission": r["permission_status"],
                    "usable": r["permission_status"] == "authorized" and r["rights_id"] is not None})
    return sorted(out, key=lambda o: -o["score"])[:limit]


def creator_report(conn, limit: int = 50) -> list[dict]:
    return conn.execute(
        """SELECT cr.id, cr.display_name, cr.permission_status, cr.permission_summary, cr.rights_id,
                  s.total AS score, s.audience, s.engagement, s.viral, s.gta_focus, s.live_now,
                  s.detail->>'dominant_language' AS language, (s.detail->>'gta_factor')::float AS gta_factor,
                  (s.detail->>'language_fit')::float AS language_fit,
                  (SELECT json_agg(json_build_object('platform', cc.platform, 'handle', cc.handle, 'followers', cc.followers,
                                                     'url', cc.url)) FROM creator_channels cc WHERE cc.creator_id = cr.id) AS channels,
                  (SELECT count(*) FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                    WHERE cc.creator_id = cr.id AND ct.kind = 'live') AS live_now_count,
                  (SELECT count(*) FROM creator_content ct JOIN creator_channels cc ON cc.id = ct.channel_id
                    WHERE cc.creator_id = cr.id AND ct.game = 'gta6') AS gta6_items
           FROM creators cr
           LEFT JOIN LATERAL (SELECT * FROM creator_scores WHERE creator_id = cr.id ORDER BY scored_at DESC LIMIT 1) s ON TRUE
           ORDER BY s.total DESC NULLS LAST, cr.id LIMIT %s""", (limit,)).fetchall()


def run_all(conn, cfg: dict, yt, tw, notes: list[str]) -> dict:
    """discover -> monitor -> permissions -> rank, for the scheduled n8n workflow."""
    d = discover(conn, cfg, yt, tw) if (yt or tw) else {"skipped": True}
    m = monitor(conn, cfg, yt, tw) if (yt or tw) else {"skipped": True}
    p = check_permissions(conn)
    r = rank(conn, cfg)
    return {"notes": notes, "discover": d, "monitor": m,
            "permissions": {s: sum(1 for x in p if x["status"] == s) for s in {x["status"] for x in p}},
            "top": r[:10], "date": date.today().isoformat()}
