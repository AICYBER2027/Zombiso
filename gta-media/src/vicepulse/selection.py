"""Highlight selection: triage every new candidate, then pick the day's clips per channel."""

from __future__ import annotations

from datetime import date

from . import db, dedupe, rights
from .config import Settings
from .llm import LLM

FINALISTS = 15


def triage_new(conn, settings: Settings, llm: LLM) -> dict:
    """Score every new candidate once (shared by EN and BR). Blocked tags stop a candidate here."""
    block = set(settings.game_config()["block_tags"])
    rows = conn.execute(
        """SELECT c.*, s.file_path, s.game FROM candidates c JOIN sources s ON s.id = c.source_id
           WHERE c.status = 'new' AND s.status = 'analysed' ORDER BY c.id""").fetchall()
    counts = {"triaged": 0, "blocked": 0}
    for c in rows:
        rights.require_cleared_source(conn, c["source_id"])
        hashes, frames = dedupe.keyframe_hashes(c["file_path"], c["start_s"], c["end_s"],
                                                settings.dir("frames"), f"cand{c['id']}")
        result = llm.triage(dict(c), frames, game=c["game"])
        flagged = sorted(block.intersection(result["policy_tags"]))
        status = "blocked" if flagged else "triaged"
        conn.execute(
            """UPDATE candidates SET triage = %s, triage_score = %s, keyframe_hashes = %s, status = %s,
               status_reason = %s WHERE id = %s""",
            (db.Jsonb(result), result["hook_score"] + result["clarity_score"], hashes, status,
             ("blocked tags: " + ", ".join(flagged)) if flagged else None, c["id"]))
        db.log_event(conn, "triage", "candidate", c["id"], status, {"tags": result["policy_tags"]})
        counts[status] += 1
    conn.commit()
    return counts


def _examples(conn, channel_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT cl.selection_reason, cl.script->>'angle' AS angle, sum(m.views) AS views
           FROM clips cl JOIN publish_jobs p ON p.clip_id = cl.id JOIN metrics m ON m.publish_job_id = p.id
           WHERE cl.channel_id = %s AND m.captured_on >= CURRENT_DATE - 7
           GROUP BY cl.id ORDER BY views DESC""", (channel_id,)).fetchall()
    best, worst = rows[:3], rows[-3:] if len(rows) > 3 else []
    return [dict(r, label="best") for r in best] + [dict(r, label="worst") for r in worst]


def select_for_channel(conn, settings: Settings, llm: LLM, channel_key: str, for_date: date,
                       max_clips: int | None = None, source_id: int | None = None,
                       ignore_day_limit: bool = False) -> dict:
    channel_cfg = settings.channel(channel_key)
    ch = conn.execute("SELECT * FROM channels WHERE key = %s", (channel_key,)).fetchone()
    have = conn.execute(
        """SELECT count(*) AS n FROM clips WHERE channel_id = %s AND for_date = %s
           AND status NOT IN ('duplicate', 'rejected', 'failed_check', 'blocked', 'taken_down')""",
        (ch["id"], for_date)).fetchone()["n"]
    # A highlight the owner asks for by hand is made even when the day's automatic slots are taken.
    need = max_clips or 1 if ignore_day_limit else max(0, ch["clips_per_day"] - have)
    if max_clips is not None:
        need = min(need, max(0, max_clips))
    if need == 0:
        return {"channel": channel_key, "selected": [], "duplicates": [], "note": "day already full"}
    finalists = conn.execute(
        """SELECT c.id, c.start_s, c.end_s, c.transcript, c.triage_score, c.triage->>'summary' AS summary,
                  c.keyframe_hashes, s.game
           FROM candidates c JOIN sources s ON s.id = c.source_id
           WHERE c.status IN ('triaged', 'selected') AND s.status = 'analysed'
             AND (%s::int IS NULL OR c.source_id = %s::int)
             AND NOT EXISTS (SELECT 1 FROM clips cl WHERE cl.candidate_id = c.id AND cl.channel_id = %s)
           ORDER BY c.triage_score DESC NULLS LAST LIMIT %s""", (source_id, source_id, ch["id"], FINALISTS)).fetchall()
    # Layers 1-2 of duplicate prevention before the model sees anything: candidates that repeat a
    # clip this channel already has are never offered.
    filtered = []
    offered = []
    for f in finalists:
        dup = dedupe.check_footage(conn, channel_id=ch["id"], candidate_id=f["id"], in_s=f["start_s"], out_s=f["end_s"])
        if dup.duplicate:
            filtered.append({"candidate_id": f["id"], "reasons": dup.reasons})
        else:
            offered.append(f)
    finalists = offered
    if not finalists:
        return {"channel": channel_key, "selected": [], "duplicates": [], "filtered": filtered, "note": "no candidates"}
    for f in finalists:
        f["frames"] = sorted(settings.dir("frames").glob(f"cand{f['id']}_*.jpg"))
    topics = [r["topic"] for r in conn.execute(
        "SELECT topic FROM topics WHERE language = %s AND seen_on = %s ORDER BY score DESC LIMIT 10",
        (ch["language"], for_date)).fetchall()]
    result = llm.select(channel_cfg, [dict(f) for f in finalists], need, examples=_examples(conn, ch["id"]),
                        topics=topics, game=settings.game)
    by_id = {f["id"]: f for f in finalists}
    selected, duplicates = [], []
    for pick in result["picks"]:
        if len(selected) >= need:
            break
        cand = by_id.get(pick["candidate_id"])
        if cand is None:
            continue  # the model named an id it was not offered
        in_s = max(cand["start_s"], min(pick["in_s"], cand["end_s"]))
        out_s = min(cand["end_s"], max(pick["out_s"], in_s + 1))
        dup = dedupe.check_footage(conn, channel_id=ch["id"], candidate_id=cand["id"], in_s=in_s, out_s=out_s)
        status = "duplicate" if dup.duplicate else "selected"
        clip = conn.execute(
            """INSERT INTO clips (candidate_id, channel_id, language, for_date, in_s, out_s, selection_reason,
                                  status, status_reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (cand["id"], ch["id"], ch["language"], for_date, in_s, out_s, pick["reason"], status,
             "; ".join(dup.reasons) or None)).fetchone()
        conn.execute("UPDATE candidates SET status = 'selected' WHERE id = %s AND status = 'triaged'", (cand["id"],))
        db.log_event(conn, "select", "clip", clip["id"], status, {"reasons": dup.reasons, "channel": channel_key})
        (duplicates if dup.duplicate else selected).append(clip["id"])
    conn.commit()
    return {"channel": channel_key, "selected": selected, "duplicates": duplicates, "filtered": filtered}
