"""Command line for every pipeline stage. Each command prints one JSON object, so n8n can read it.

    vicepulse <command> [options]

Exit code 0 = the stage ran (read the JSON for what happened); non-zero = the stage itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

from . import autosource, commentary, db, discovery, ingest, policy, publish, render, selection, signals
from .config import Settings
from .llm import make_llm


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def _ids(text: str | None) -> list[int] | None:
    return [int(x) for x in text.split(",")] if text else None


def _day(text: str | None) -> date:
    return date.fromisoformat(text) if text else datetime.now(timezone.utc).date()


def cmd_migrate(a, s, conn):
    return {"applied": db.migrate(conn), "channels": [c["key"] for c in db.seed_channels(conn, s)]}


def cmd_rights_add(a, s, conn):
    if bool(a.json) == bool(a.json_file):
        raise ValueError("give exactly one of --json or --json-file")
    fields = json.loads(a.json) if a.json else json.loads(open(a.json_file, encoding="utf-8").read())
    return {"rights_id": ingest.add_rights(conn, **fields)}


def cmd_rights_verify(a, s, conn):
    row = conn.execute("UPDATE rights_ledger SET verified_by = %s, verified_at = now() WHERE id = %s RETURNING id",
                       (a.by, a.id)).fetchone()
    db.log_event(conn, "rights", "rights_ledger", a.id, "verified", {"by": a.by})
    conn.commit()
    return {"verified": bool(row), "requarantine_check": ingest.recheck_quarantine(conn)}


def cmd_rights_revoke(a, s, conn):
    conn.execute("UPDATE rights_ledger SET revoked_at = now(), revoked_reason = %s WHERE id = %s", (a.reason, a.id))
    sources = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE rights_id = %s", (a.id,)).fetchall()]
    conn.commit()
    return {"revoked": a.id, "takedowns": [publish.take_down(conn, s, source_id=sid, reason=f"licence revoked: {a.reason}")
                                          for sid in sources]}


def cmd_ingest(a, s, conn):
    return ingest.ingest_file(conn, s, a.file, origin=a.origin, rights_id=a.rights_id, game=a.game)


def cmd_signals(a, s, conn):
    ids = [a.source_id] if a.source_id else [r["id"] for r in conn.execute(
        "SELECT id FROM sources WHERE status = 'cleared' ORDER BY id").fetchall()]
    if not 15 <= a.clip_seconds <= 60:
        raise ValueError("--clip-seconds must be between 15 and 60")
    return {"sources": [signals.extract(conn, s, i, window_s=a.clip_seconds) for i in ids]}


def cmd_select(a, s, conn):
    llm = make_llm(s)
    out = {"triage": selection.triage_new(conn, s, llm), "channels": []}
    for ch in ([a.channel] if a.channel else [c["key"] for c in s.channels()]):
        out["channels"].append(selection.select_for_channel(conn, s, llm, ch, _day(a.date),
                                                            max_clips=getattr(a, "max_clips", None),
                                                            source_id=getattr(a, "source_id", None)))
    return out


def cmd_script(a, s, conn):
    return commentary.write_scripts(conn, s, make_llm(s), clip_ids=_ids(a.clips))


def cmd_render(a, s, conn):
    return render.render_pending(conn, s, clip_ids=_ids(a.clips), layout=a.layout, style=a.style)


def _facecam(text: str | None) -> dict | None:
    """'x,y,w,h' (fractions of the source frame, e.g. 0.02,0.6,0.25,0.35) plus optional ':split' or ':pip'."""
    if not text:
        return None
    if text == "auto":
        return "auto"
    box, _, mode = text.partition(":")
    x, y, w, h = (float(v) for v in box.split(","))
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-6 and 0 < h <= 1 - y + 1e-6):
        raise ValueError("--facecam must be x,y,w,h fractions inside the frame")
    return {"x": x, "y": y, "w": w, "h": h, "mode": mode or "auto"}


def cmd_style_compare(a, s, conn):
    """Render one highlight in several edit styles, side by side, with timings and quality checks."""
    from . import styles

    if a.clip:
        clip = conn.execute(
            """SELECT cl.*, ch.key AS channel_key, c.source_id, s.file_path FROM clips cl
               JOIN channels ch ON ch.id = cl.channel_id JOIN candidates c ON c.id = cl.candidate_id
               JOIN sources s ON s.id = c.source_id WHERE cl.id = %s""", (a.clip,)).fetchone()
        if clip is None:
            raise ValueError(f"unknown clip {a.clip}")
        from . import rights

        rights.require_cleared_source(conn, clip["source_id"])      # only licensed footage
        src, in_s, out_s = clip["file_path"], clip["in_s"], clip["out_s"]
        cues = (clip["script"] or {}).get("captions", [])
        brand = render.channel_brand(s, clip["channel_key"])
    else:
        if not a.own_test_footage:
            raise ValueError("--file needs --own-test-footage (you made it yourself), or use --clip for licensed footage")
        src, in_s, out_s = a.file, a.start, a.end
        cues = json.loads(open(a.captions_json, encoding="utf-8").read()) if a.captions_json else []
        if a.transcribe and not cues:
            # The clip's real speech as captions (local Whisper, free).
            tr = signals.WhisperTranscriber(s.whisper_model, cache_dir=os.environ.get("WHISPER_CACHE_DIR"))
            end = a.end if a.end is not None else render.media.probe(src)["duration"]
            cues = commentary.transcript_cues(tr.transcribe(src, start=in_s, duration=end - in_s), end - in_s)
        brand = render.channel_brand(s, a.channel) if a.channel else {}
    brand = dict(brand or {})
    if a.creator_name:
        brand["creator_name"] = a.creator_name
    if a.logo:
        brand["logo"] = a.logo
    return render.compare_styles(src, in_s=in_s, out_s=out_s, cues=cues, styles_=a.styles.split(","),
                                 out_dir=a.out_dir or str(s.dir("work", "style-compare")), size=a.size,
                                 encoder=a.encoder, platform=a.platform, brand=brand, facecam=_facecam(a.facecam),
                                 layout=a.layout)


def cmd_check(a, s, conn):
    return policy.final_check(conn, s, make_llm(s), clip_ids=_ids(a.clips))


def cmd_daily(a, s, conn):
    """Select, script, render and check in one go (workflow 3)."""
    out = {"select": cmd_select(a, s, conn)}
    out["script"] = commentary.write_scripts(conn, s, make_llm(s))
    out["render"] = render.render_pending(conn, s, layout=a.layout)
    out["check"] = policy.final_check(conn, s, make_llm(s))
    return out


def cmd_highlight(a, s, conn):
    if not 15 <= a.clip_seconds <= 60:
        raise ValueError("--clip-seconds must be between 15 and 60")
    return autosource.highlight(conn, s, make_llm(s), a.file, rights=a.rights, channel=a.channel,
                                clip_seconds=a.clip_seconds, licence=a.licence, author=a.author, url=a.url,
                                creator=a.creator)


def cmd_youtube_client(a, s, conn):
    from . import accounts

    url = (os.environ.get("VICEPULSE_REVIEW_URL") or "http://localhost:8090").rstrip("/")
    out = accounts.import_google_client_json(s.data_dir, a.json_file, f"{url}/oauth/youtube/callback")
    out["live_publishing"] = "on" if s.allow_live_publish else "off"
    return out


def cmd_accounts_check(a, s, conn):
    from . import accounts

    return accounts.verify(s)


def cmd_live_watch(a, s, conn):
    """Watch one Twitch broadcaster and clip spikes while they are live (runs until the stream ends)."""
    from . import live

    tw = live.TwitchLive(s)
    row = live.start_session(conn, s, tw, a.twitch, a.channel, creator_id=a.creator_id, own=a.own,
                             also_channels=_also(a.also_channel))
    det = live.SpikeDetector(threshold=a.threshold)
    runner = live.LiveRunner(s, row, tw=tw, detector=det, chat_latency_s=a.chat_latency)
    print(json.dumps({"watching": row["login"], "session": row["id"], "channel": a.channel,
                      "live_publishing": "on" if s.allow_live_publish else "off"}), flush=True)
    return runner.run(until_end=not a.keep_waiting, max_hours=a.max_hours)


def cmd_telegram_setup(a, s, conn):
    """Find your Telegram chat id (send any message to your bot first); with --test, send a test message."""
    from . import notify

    if a.test:
        return {"sent": notify.send("VicePulse test: notifications work.")}
    return {"chats": notify.find_chat_ids(),
            "next": "put the chat_id in .env as TELEGRAM_CHAT_ID, restart, then run telegram-setup --test"}


def cmd_live_status(a, s, conn):
    from . import live

    return live.status(conn)


def _also(value: str | None) -> list[str]:
    return [c.strip() for c in (value or "").split(",") if c.strip()]


def cmd_feed_add(a, s, conn):
    """Register a direct feed; prints the OBS server + stream key once (only a hash is stored)."""
    from . import feeds

    row = feeds.add_feed(conn, s, a.name, a.display_name or a.name, a.channel, creator_id=a.creator_id, own=a.own,
                         also_channels=_also(a.also_channel))
    return {"feed": row["slug"], "channels": row["channels"], "obs": row["obs"],
            "keep_private": "the stream key works like a password; it is not shown again (feed-key makes a new one)"}


def cmd_feed_key(a, s, conn):
    from . import feeds

    return {"feed": a.name, "obs": feeds.new_key(conn, a.name, s)}


def cmd_feed_list(a, s, conn):
    rows = conn.execute("""SELECT slug, display_name, channel_key, channels, enabled, last_auth_at, last_auth_ok, created_at
                           FROM live_feeds ORDER BY id""").fetchall()
    return {"feeds": rows}


def cmd_live_run(a, s, conn):
    """Watch every listed direct feed (and Twitch streamers) in one process, with one shared editing queue."""
    import threading

    from . import feeds, live

    runners = []
    slugs = [r["slug"] for r in conn.execute("SELECT slug FROM live_feeds WHERE enabled ORDER BY id").fetchall()] \
        if a.feeds == "all" else _also(a.feeds)
    rx = feeds.Receiver(api=a.receiver_api, rtsp=a.receiver_rtsp, rec_dir=s.data_dir / "feeds" / "rec")
    for slug in slugs:
        row = feeds.start_session(conn, s, slug)
        runners.append(feeds.FeedRunner(s, row, receiver=rx, grace_s=a.grace,
                                        detector=feeds.AudioSpikeDetector(threshold=a.threshold)))
    for spec in a.twitch or []:                        # LOGIN:CHANNEL[:CREATOR_ID|own][:ALSO]
        parts = spec.split(":")
        if len(parts) < 3:
            raise ValueError("--twitch LOGIN:CHANNEL:CREATOR_ID (or LOGIN:CHANNEL:own), optionally :br for a 2nd channel")
        login, ch, who = parts[:3]
        tw = live.TwitchLive(s)
        row = live.start_session(conn, s, tw, login, ch, creator_id=None if who == "own" else int(who),
                                 own=who == "own", also_channels=_also(parts[3] if len(parts) > 3 else None))
        runners.append(live.LiveRunner(s, row, tw=tw))
    if not runners:
        raise ValueError("nothing to watch: add a feed (feed-add) or pass --twitch")
    print(json.dumps({"watching": [r.s["login"] for r in runners], "editing": "one shared queue",
                      "live_publishing": "on" if s.allow_live_publish else "off"}), flush=True)
    results: list = []
    threads = [threading.Thread(target=lambda r=r: results.append(r.run(until_end=False, max_hours=a.max_hours)),
                                daemon=True) for r in runners]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for r in runners:
            r.stop.set()
        for t in threads:
            t.join(timeout=60)
    return {"results": results}


def cmd_autoclip(a, s, conn):
    if not 15 <= a.clip_seconds <= 60:
        raise ValueError("--clip-seconds must be between 15 and 60")
    return autosource.run(conn, s, make_llm(s), discovery.load_config(), sources=a.source,
                          channels=a.channel.split(",") if a.channel else None, clip_seconds=a.clip_seconds,
                          max_clips=a.max_clips)


def cmd_queue(a, s, conn):
    return {"pending": policy.approval_queue(conn)}


def cmd_decide(a, s, conn):
    return policy.decide(conn, a.clip, approve=a.decision == "approve", by=a.by, reason=a.reason,
                         tiktok_options=json.loads(a.tiktok_options) if a.tiktok_options else None)


def cmd_schedule(a, s, conn):
    return publish.schedule_approved(conn, s)


def cmd_publish(a, s, conn):
    return publish.run_due(conn, s)


def cmd_analytics(a, s, conn):
    return publish.pull_analytics(conn, s)


def cmd_takedown(a, s, conn):
    return publish.take_down(conn, s, clip_id=a.clip, source_id=a.source, reason=a.reason)


def cmd_health(a, s, conn):
    return publish.health(conn, s)


def cmd_topics_add(a, s, conn):
    """Store topics found by the trend scan. Input: JSON list of {topic, source, score, language}."""
    n = 0
    for t in json.loads(a.json):
        conn.execute(
            """INSERT INTO topics (game, language, topic, source, score) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (game, language, topic, seen_on) DO UPDATE SET score = EXCLUDED.score""",
            (s.game, t.get("language", "en"), t["topic"][:300], t.get("source", "manual"), float(t.get("score", 0))))
        n += 1
    conn.commit()
    return {"topics": n}


def cmd_prospects_add(a, s, conn):
    """Store creators to contact for a licence. Input: JSON list of {platform, channel_ref, display_name, ...}."""
    n = 0
    for p in json.loads(a.json):
        conn.execute(
            """INSERT INTO creator_prospects (platform, channel_ref, display_name, language, subscribers, fit_score)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (platform, channel_ref) DO UPDATE
               SET subscribers = EXCLUDED.subscribers, fit_score = EXCLUDED.fit_score""",
            (p["platform"], p["channel_ref"], p.get("display_name"), p.get("language"), p.get("subscribers"),
             p.get("fit_score")))
        n += 1
    conn.commit()
    return {"prospects": n}


def _disc(conn):
    cfg = discovery.load_config()
    yt, tw, notes = discovery.make_clients(conn, cfg)
    return cfg, yt, tw, notes


def _only(a, yt, tw):
    """--only youtube / --only twitch: use one platform, e.g. to test Twitch without spending YouTube quota."""
    only = getattr(a, "only", None)
    return (yt if only in (None, "youtube") else None), (tw if only in (None, "twitch") else None)


def cmd_discover(a, s, conn):
    cfg, yt, tw, notes = _disc(conn)
    yt, tw = _only(a, yt, tw)
    return {"notes": notes, **(discovery.discover(conn, cfg, yt, tw) if (yt or tw) else {"skipped": True})}


def cmd_monitor(a, s, conn):
    cfg, yt, tw, notes = _disc(conn)
    yt, tw = _only(a, yt, tw)
    return {"notes": notes, **(discovery.monitor(conn, cfg, yt, tw) if (yt or tw) else {"skipped": True})}


def cmd_rank(a, s, conn):
    return {"ranking": discovery.rank(conn, discovery.load_config())}


def cmd_permissions(a, s, conn):
    return {"creators": discovery.check_permissions(conn)}


def cmd_creators(a, s, conn):
    return {"creators": discovery.creator_report(conn, a.limit, a.include_excluded)}


def cmd_sources_check(a, s, conn):
    return discovery.sources_check(conn, discovery.load_config())


def cmd_languages(a, s, conn):
    cfg = discovery.load_config()
    out = discovery.classify_languages(conn, cfg)
    out["pruned"] = discovery.prune_untargeted(conn, cfg)
    return out


def cmd_opportunities(a, s, conn):
    return {"opportunities": discovery.opportunities(conn, a.limit, a.per_creator)}


def cmd_creator_review(a, s, conn):
    return discovery.review_creator(
        conn, a.creator, decision=a.decision, by=a.by, evidence_url=a.evidence_url, notes=a.notes,
        platforms=a.platforms.split(",") if a.platforms else None,
        languages=a.languages.split(",") if a.languages else None,
        monetisation_allowed=a.monetisation_allowed, edits_allowed=a.edits_allowed, expires_on=a.expires_on,
        credit_text=a.credit_text)


def cmd_discovery_run(a, s, conn):
    cfg, yt, tw, notes = _disc(conn)
    return discovery.run_all(conn, cfg, yt, tw, notes)


def _secret(env_name: str, prompt: str) -> str:
    """Read a secret from an environment variable or a hidden prompt; never from a command-line argument."""
    value = os.environ.get(env_name, "")
    if not value and sys.stdin.isatty():
        import getpass

        value = getpass.getpass(prompt)
    if not value:
        raise ValueError(f"set {env_name} or run interactively to type it (it is never shown or logged)")
    return value.strip()


def cmd_publish_queue(a, s, conn):
    return publish.queue_report(conn)


def cmd_publish_requeue(a, s, conn):
    return publish.requeue(conn, a.job, reason=a.reason)


def cmd_facebook_connect(a, s, conn):
    from . import accounts

    token = _secret("FACEBOOK_SETUP_TOKEN", "Meta system-user token (hidden): ")
    expected = a.page_name or s.channel(a.channel).get("facebook_page")
    return {"channel": a.channel, **accounts.facebook_save_page(s.data_dir, a.channel, token, page_id=a.page_id or "",
                                                                expected_name=None if a.page_id else expected)}


def cmd_staff_create(a, s, conn):
    from . import clients

    pw = _secret("VICEPULSE_STAFF_PASSWORD", "New staff password (hidden): ")
    return {"staff_user": clients.create_staff(conn, email=a.email, password=pw), "email": a.email.lower()}


def cmd_clients(a, s, conn):
    rows = conn.execute("""SELECT c.id, c.slug, c.display_name, c.language, c.status, c.plan_key, c.created_at,
                                  (SELECT count(*) FROM client_deliveries d WHERE d.client_id = c.id) AS deliveries,
                                  (SELECT count(*) FROM client_permissions p WHERE p.client_id = c.id
                                     AND p.status = 'requested') AS open_permission_requests
                           FROM clients c WHERE c.slug <> 'vicepulse-staff' ORDER BY c.id""").fetchall()
    return {"clients": rows}


def cmd_client_deliver(a, s, conn):
    """Attach a finished clip file to a client's dashboard (copied into the data volume)."""
    import shutil
    from pathlib import Path

    from . import clients, media

    c = conn.execute("SELECT * FROM clients WHERE slug = %s", (a.client,)).fetchone()
    if c is None:
        raise ValueError(f"no client {a.client!r}")
    src = Path(a.file)
    if not src.is_file():
        raise FileNotFoundError(src)
    dest_dir = s.dir("clients", str(c["id"]), "deliveries")
    dest = dest_dir / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{src.stem[:40]}.mp4"
    shutil.copyfile(src, dest)
    info = media.probe(dest)
    did = clients.add_delivery(conn, client_id=c["id"], file_path=str(dest), title=a.title, style=a.style,
                               clip_id=a.clip, duration_s=info.get("duration"), file_sha256=media.sha256_file(dest))
    return {"delivery": did, "client": a.client, "file": str(dest)}


def cmd_client_render(a, s, conn):
    cues = json.loads(open(a.captions_json, encoding="utf-8").read()) if a.captions_json else []
    return render.render_for_client(conn, s, a.client, a.file, in_s=a.start, out_s=a.end, cues=cues, title=a.title,
                                    size=a.size, encoder=a.encoder)


def cmd_web_build(a, s, conn):
    from pathlib import Path

    from .web.site import build_static

    files = build_static(s, Path(a.out), app_url=a.app_url)
    return {"out": a.out, "files": len(files), "uploaded": False,
            "note": "static files only; nothing was published. Deploy only after the owner approves."}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vicepulse", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("migrate", cmd_migrate, "apply database migrations and seed the EN / BR channels")
    sp = add("rights-add", cmd_rights_add, "add a rights ledger entry (unverified until rights-verify)")
    sp.add_argument("--json", help="ledger fields as JSON")
    sp.add_argument("--json-file", help="path to a JSON file with the ledger fields (easier from Windows PowerShell)")
    sp = add("rights-verify", cmd_rights_verify, "record that a person checked the signed licence")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--by", required=True)
    sp = add("rights-revoke", cmd_rights_revoke, "revoke a licence and take down everything cut from it")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--reason", required=True)
    sp = add("ingest", cmd_ingest, "register a footage file and run the rights gate")
    sp.add_argument("--file", required=True)
    sp.add_argument("--origin", required=True, choices=["own_recording", "creator_upload"])
    sp.add_argument("--rights-id", type=int)
    sp.add_argument("--game")
    sp = add("signals", cmd_signals, "extract scene / loudness / transcript signals for cleared sources")
    sp.add_argument("--source-id", type=int)
    sp.add_argument("--clip-seconds", type=float, default=20.0, help="candidate clip length, 15-60 seconds (default 20)")
    for name, fn, help_ in (("select", cmd_select, "triage candidates and select the day's clips per channel"),
                            ("daily", cmd_daily, "select + script + render + final check")):
        sp = add(name, fn, help_)
        sp.add_argument("--channel")
        sp.add_argument("--date")
        sp.add_argument("--max-clips", type=int, help="at most this many new clips per channel (default: the daily quota)")
        sp.add_argument("--source-id", type=int, help="only choose moments from this footage file")
        if name == "daily":
            sp.add_argument("--layout", default="auto", choices=["auto", "crop", "blur"])
    sp = add("script", cmd_script, "write commentary, captions and titles")
    sp.add_argument("--clips")
    sp = add("render", cmd_render, "render 1080x1920 files with burned captions")
    sp.add_argument("--clips")
    sp.add_argument("--layout", default="auto", choices=["auto", "crop", "blur"])
    sp.add_argument("--style", help="cinematic | viral | clean (default VICEPULSE_EDIT_STYLE, else the classic template)")
    sp = add("style-compare", cmd_style_compare, "render one highlight in the Cinematic, Funny/Viral and Clean styles")
    sp.add_argument("--clip", type=int, help="a scripted clip from licensed footage")
    sp.add_argument("--file", help="a video you made yourself (needs --own-test-footage)")
    sp.add_argument("--own-test-footage", action="store_true")
    sp.add_argument("--start", type=float, default=0.0)
    sp.add_argument("--end", type=float)
    sp.add_argument("--captions-json", help="caption cues [{start,end,text,kind}] for --file")
    sp.add_argument("--channel", choices=["en", "br"], help="use this channel's branding")
    sp.add_argument("--styles", default="cinematic,viral,clean,pro")
    sp.add_argument("--creator-name", help="name shown on the facecam frame and titles")
    sp.add_argument("--logo", help="creator logo image (PNG) for the badge")
    sp.add_argument("--size", default="720x1280")
    sp.add_argument("--encoder", default="auto", choices=["auto", "nvenc", "libx264"])
    sp.add_argument("--platform", default="shared", choices=["shared", "youtube", "instagram", "tiktok"])
    sp.add_argument("--layout", default="auto", choices=["auto", "crop", "blur"],
                    help="crop = follow the action in a 9:16 crop; blur = whole frame on a blurred fill")
    sp.add_argument("--facecam", help="auto (detect it), or x,y,w,h in fractions of the picture, plus :pip or :split")
    sp.add_argument("--transcribe", action="store_true", help="captions from the clip's real speech (Whisper)")
    sp.add_argument("--out-dir")
    sp = add("check", cmd_check, "final policy and quality check, then queue for approval")
    sp.add_argument("--clips")
    add("accounts-check", cmd_accounts_check, "check which YouTube/Instagram account each stored login opens (read-only)")
    sp = add("youtube-client", cmd_youtube_client,
             "save the Google OAuth client from its downloaded JSON file (secret is stored, never printed)")
    sp.add_argument("--json-file", required=True)
    sp = add("highlight", cmd_highlight, "one subtitled vertical highlight from one file you point at (no publishing)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--rights", required=True, choices=["own", "cc", "permission"])
    sp.add_argument("--licence", default="", help="cc: licence name, e.g. 'CC BY 4.0'")
    sp.add_argument("--author", default="", help="cc: the author to credit")
    sp.add_argument("--url", default="", help="cc: licence/file page; permission: link to the written permission")
    sp.add_argument("--creator", default="", help="permission: the creator's name")
    sp.add_argument("--channel", default="en", choices=["en", "br"])
    sp.add_argument("--clip-seconds", type=float, default=45.0)
    sp = add("live-watch", cmd_live_watch, "watch a Twitch streamer and clip chat spikes while live (official API)")
    sp.add_argument("--twitch", required=True, help="the Twitch login (twitch.tv/<login>)")
    sp.add_argument("--channel", required=True, choices=["en", "br"])
    who = sp.add_mutually_exclusive_group(required=True)
    who.add_argument("--creator-id", type=int, help="the authorized creator (Creators tab)")
    who.add_argument("--own", action="store_true", help="your own stream")
    sp.add_argument("--threshold", type=float, default=0.55, help="spike score needed (0-1)")
    sp.add_argument("--chat-latency", type=float, default=8.0,
                    help="seconds between the moment on stream and the chat reacting (shifts the 20 s/60 s window)")
    sp.add_argument("--keep-waiting", action="store_true", help="keep watching after the stream ends")
    sp.add_argument("--max-hours", type=float, default=16.0)
    sp.add_argument("--also-channel", help="extra channels that also get a version, e.g. br")
    add("live-status", cmd_live_status, "live sessions and detected moments")
    sp = add("feed-add", cmd_feed_add, "register a creator's direct live feed (second OBS output); prints the key once")
    sp.add_argument("--name", required=True, help="short feed name, e.g. burgao")
    sp.add_argument("--display-name")
    sp.add_argument("--channel", required=True, choices=["en", "br"])
    sp.add_argument("--also-channel", help="extra channels that also get a version, e.g. br")
    who = sp.add_mutually_exclusive_group(required=True)
    who.add_argument("--creator-id", type=int, help="the authorized creator (Creators tab)")
    who.add_argument("--own", action="store_true", help="your own stream or test footage")
    sp = add("feed-key", cmd_feed_key, "replace a feed's stream key")
    sp.add_argument("--name", required=True)
    add("feed-list", cmd_feed_list, "list direct feeds (no keys)")
    sp = add("live-run", cmd_live_run, "watch direct feeds and Twitch streamers together, one shared editing queue")
    sp.add_argument("--feeds", default="all", help="'all' enabled feeds, a comma list, or 'none'")
    sp.add_argument("--twitch", action="append", help="LOGIN:CHANNEL:CREATOR_ID[:ALSO], repeatable")
    sp.add_argument("--threshold", type=float, default=0.55, help="audio spike score needed (0-1)")
    sp.add_argument("--grace", type=float, default=30.0, help="seconds a feed may drop before the session ends")
    sp.add_argument("--receiver-api", default="http://mediamtx:9997")
    sp.add_argument("--receiver-rtsp", default="rtsp://mediamtx:8554")
    sp.add_argument("--max-hours", type=float, default=24.0)
    sp = add("telegram-setup", cmd_telegram_setup, "find your Telegram chat id for clip notifications")
    sp.add_argument("--test", action="store_true", help="send a test message")
    sp = add("autoclip", cmd_autoclip, "hands-free: fetch legitimately usable footage, clip, subtitle, render, save MP4")
    sp.add_argument("--source", default="all", choices=["all", "commons", "own"])
    sp.add_argument("--channel", help="comma list of channels, default from config (en)")
    sp.add_argument("--clip-seconds", type=float, default=30.0)
    sp.add_argument("--max-clips", type=int, default=1, help="clips per file per channel")
    add("queue", cmd_queue, "list clips waiting for approval")
    sp = add("decide", cmd_decide, "approve or reject a clip")
    sp.add_argument("--clip", type=int, required=True)
    sp.add_argument("--decision", required=True, choices=["approve", "reject"])
    sp.add_argument("--by", required=True)
    sp.add_argument("--reason")
    sp.add_argument("--tiktok-options", help="JSON with the reviewer's TikTok choices")
    add("schedule", cmd_schedule, "create publish jobs for approved clips")
    add("publish", cmd_publish, "post whatever is due (dry run unless live publishing is enabled)")
    add("analytics", cmd_analytics, "pull metrics for published posts")
    sp = add("takedown", cmd_takedown, "remove a clip or a whole source everywhere")
    sp.add_argument("--clip", type=int)
    sp.add_argument("--source", type=int)
    sp.add_argument("--reason", required=True)
    add("health", cmd_health, "list alerts: failed posts, expiring licences, short days")
    sp = add("topics-add", cmd_topics_add, "store trend-scan topics")
    sp.add_argument("--json", required=True)
    sp = add("prospects-add", cmd_prospects_add, "store creator prospects")
    sp.add_argument("--json", required=True)
    sp = add("discover", cmd_discover, "find GTA creators on YouTube and Twitch (official APIs, read-only)")
    sp.add_argument("--only", choices=["youtube", "twitch"])
    sp = add("monitor", cmd_monitor, "refresh tracked creators: new uploads, scheduled and live streams")
    sp.add_argument("--only", choices=["youtube", "twitch"])
    add("rank", cmd_rank, "score creators by audience, engagement, viral potential, GTA focus and live status")
    add("permissions", cmd_permissions, "scan creators' public text for clipping / reupload statements")
    sp = add("creators", cmd_creators, "list ranked creators with their permission status")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--include-excluded", action="store_true", help="also list channels excluded for language")
    add("sources-check", cmd_sources_check, "test the YouTube key and Twitch credentials (no secrets printed)")
    add("languages", cmd_languages, "detect channel languages; stop monitoring confirmed non-EN/PT channels")
    sp = add("opportunities", cmd_opportunities, "GTA livestreams now and fresh GTA uploads, ranked by reach")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--per-creator", type=int, default=2)
    sp = add("creator-review", cmd_creator_review, "a person's permission decision for a creator")
    sp.add_argument("--creator", type=int, required=True)
    sp.add_argument("--decision", required=True, choices=["authorize", "deny", "needs_info"])
    sp.add_argument("--by", required=True)
    sp.add_argument("--evidence-url", help="https link to the saved licence / written permission / archived policy")
    sp.add_argument("--notes")
    sp.add_argument("--platforms", help="comma list, default youtube,instagram,tiktok")
    sp.add_argument("--languages", help="comma list, default all")
    sp.add_argument("--monetisation-allowed", action="store_true")
    sp.add_argument("--edits-allowed", action="store_true")
    sp.add_argument("--expires-on")
    sp.add_argument("--credit-text")
    add("discovery-run", cmd_discovery_run, "discover + monitor + permissions + rank (scheduled job)")
    add("publish-queue", cmd_publish_queue, "publishing queue: counts per platform and the latest errors")
    sp = add("publish-requeue", cmd_publish_requeue, "put a failed/blocked job back in the queue after checking it by hand")
    sp.add_argument("--job", type=int, required=True)
    sp.add_argument("--reason", required=True)
    sp = add("facebook-connect", cmd_facebook_connect,
             "find a channel's Facebook Page for a Meta token (FACEBOOK_SETUP_TOKEN or hidden prompt) and save its Page token")
    sp.add_argument("--channel", required=True)
    sp.add_argument("--page-id", help="pick the Page by id instead of by name")
    sp.add_argument("--page-name", help="Page name (default: facebook_page in channels.json)")
    sp = add("staff-create", cmd_staff_create, "create a VicePulse staff login for the website (password from "
             "VICEPULSE_STAFF_PASSWORD or a hidden prompt)")
    sp.add_argument("--email", required=True)
    add("clients", cmd_clients, "list creator clients")
    sp = add("client-deliver", cmd_client_deliver, "put a finished clip in a client's dashboard")
    sp.add_argument("--client", required=True, help="client slug")
    sp.add_argument("--file", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--style", choices=["cinematic", "viral", "clean", "pro"])
    sp.add_argument("--clip", type=int)
    sp = add("client-render", cmd_client_render, "edit a moment for a creator client in their style and branding "
             "(needs their verified clip_creation permission) and deliver it to their dashboard")
    sp.add_argument("--client", required=True, help="client slug")
    sp.add_argument("--file", required=True)
    sp.add_argument("--start", type=float, default=0.0)
    sp.add_argument("--end", type=float)
    sp.add_argument("--title", required=True)
    sp.add_argument("--captions-json")
    sp.add_argument("--size", help="override the plan's size, e.g. 720x1280")
    sp.add_argument("--encoder", default="auto")
    sp = add("web-build", cmd_web_build, "write the public website pages (EN + PT) as static files; uploads nothing")
    sp.add_argument("--out", default="site-dist")
    sp.add_argument("--app-url", default="", help="where sign-up/dashboard will live, e.g. https://app.example.com")
    sp = add("web", None, "run the website (creator accounts and dashboard) on this PC")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8091)
    sp = add("serve", None, "run the worker HTTP API that n8n calls")
    sp = add("review", None, "run the local review page (approve/reject clips, authorize creators)")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8090)
    return p


def run(argv: list[str], settings: Settings | None = None) -> dict:
    args = build_parser().parse_args(argv)
    settings = settings or Settings()
    if args.command == "serve":
        from .worker import serve

        serve(settings)
        return {}
    if args.command == "review":
        from . import review

        review.serve(settings, args.host, args.port)
        return {}
    if args.command == "web":
        from .web.site import serve as web_serve

        web_serve(settings, args.host, args.port)
        return {}
    if args.command == "web-build":
        return args.fn(args, settings, None)
    if args.command == "style-compare" and not args.clip:
        return args.fn(args, settings, None)          # own test footage: no database needed
    conn = db.connect(settings)
    try:
        return args.fn(args, settings, conn)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, default=_json_default, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
