"""Command line for every pipeline stage. Each command prints one JSON object, so n8n can read it.

    vicepulse <command> [options]

Exit code 0 = the stage ran (read the JSON for what happened); non-zero = the stage itself failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone

from . import commentary, db, discovery, ingest, policy, publish, render, selection, signals
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
    fields = json.loads(a.json)
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
    return {"sources": [signals.extract(conn, s, i) for i in ids]}


def cmd_select(a, s, conn):
    llm = make_llm(s)
    out = {"triage": selection.triage_new(conn, s, llm), "channels": []}
    for ch in ([a.channel] if a.channel else [c["key"] for c in s.channels()]):
        out["channels"].append(selection.select_for_channel(conn, s, llm, ch, _day(a.date)))
    return out


def cmd_script(a, s, conn):
    return commentary.write_scripts(conn, s, make_llm(s), clip_ids=_ids(a.clips))


def cmd_render(a, s, conn):
    return render.render_pending(conn, s, clip_ids=_ids(a.clips), layout=a.layout)


def cmd_check(a, s, conn):
    return policy.final_check(conn, s, make_llm(s), clip_ids=_ids(a.clips))


def cmd_daily(a, s, conn):
    """Select, script, render and check in one go (workflow 3)."""
    out = {"select": cmd_select(a, s, conn)}
    out["script"] = commentary.write_scripts(conn, s, make_llm(s))
    out["render"] = render.render_pending(conn, s, layout=a.layout)
    out["check"] = policy.final_check(conn, s, make_llm(s))
    return out


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


def cmd_discover(a, s, conn):
    cfg, yt, tw, notes = _disc(conn)
    return {"notes": notes, **(discovery.discover(conn, cfg, yt, tw) if (yt or tw) else {"skipped": True})}


def cmd_monitor(a, s, conn):
    cfg, yt, tw, notes = _disc(conn)
    return {"notes": notes, **(discovery.monitor(conn, cfg, yt, tw) if (yt or tw) else {"skipped": True})}


def cmd_rank(a, s, conn):
    return {"ranking": discovery.rank(conn, discovery.load_config())}


def cmd_permissions(a, s, conn):
    return {"creators": discovery.check_permissions(conn)}


def cmd_creators(a, s, conn):
    return {"creators": discovery.creator_report(conn, a.limit, a.include_excluded)}


def cmd_languages(a, s, conn):
    return discovery.classify_languages(conn, discovery.load_config())


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vicepulse", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("migrate", cmd_migrate, "apply database migrations and seed the EN / BR channels")
    sp = add("rights-add", cmd_rights_add, "add a rights ledger entry (unverified until rights-verify)")
    sp.add_argument("--json", required=True, help="ledger fields as JSON")
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
    for name, fn, help_ in (("select", cmd_select, "triage candidates and select the day's clips per channel"),
                            ("daily", cmd_daily, "select + script + render + final check")):
        sp = add(name, fn, help_)
        sp.add_argument("--channel")
        sp.add_argument("--date")
        if name == "daily":
            sp.add_argument("--layout", default="auto", choices=["auto", "crop", "blur"])
    sp = add("script", cmd_script, "write commentary, captions and titles")
    sp.add_argument("--clips")
    sp = add("render", cmd_render, "render 1080x1920 files with burned captions")
    sp.add_argument("--clips")
    sp.add_argument("--layout", default="auto", choices=["auto", "crop", "blur"])
    sp = add("check", cmd_check, "final policy and quality check, then queue for approval")
    sp.add_argument("--clips")
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
    add("discover", cmd_discover, "find GTA creators on YouTube and Twitch (official APIs, read-only)")
    add("monitor", cmd_monitor, "refresh tracked creators: new uploads, scheduled and live streams")
    add("rank", cmd_rank, "score creators by audience, engagement, viral potential, GTA focus and live status")
    add("permissions", cmd_permissions, "scan creators' public text for clipping / reupload statements")
    sp = add("creators", cmd_creators, "list ranked creators with their permission status")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--include-excluded", action="store_true", help="also list channels excluded for language")
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
    sp = add("serve", None, "run the worker HTTP API that n8n calls")
    return p


def run(argv: list[str], settings: Settings | None = None) -> dict:
    args = build_parser().parse_args(argv)
    settings = settings or Settings()
    if args.command == "serve":
        from .worker import serve

        serve(settings)
        return {}
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
