"""Final policy + quality check on the rendered clip, then the human approval queue."""

from __future__ import annotations

from . import db, media
from .config import Settings
from .llm import LLM

# What a person must decide for each TikTok post (TikTok's content sharing guidelines require
# these to be chosen by the user, with no defaults, before anything is sent).
TIKTOK_REQUIRED_OPTIONS = ("privacy_level", "allow_comment", "allow_duet", "allow_stitch", "consent_confirmed")


def final_check(conn, settings: Settings, llm: LLM, *, clip_ids: list[int] | None = None) -> dict:
    block = set(settings.game_config()["block_tags"])
    rows = conn.execute(
        """SELECT cl.*, ch.key AS channel_key, s.game FROM clips cl
           JOIN channels ch ON ch.id = cl.channel_id
           JOIN candidates c ON c.id = cl.candidate_id JOIN sources s ON s.id = c.source_id
           WHERE cl.status = 'rendered' AND (%s::int[] IS NULL OR cl.id = ANY(%s::int[])) ORDER BY cl.id""",
        (clip_ids, clip_ids)).fetchall()
    out = {"pending_approval": [], "approved": [], "failed_check": []}
    for clip in rows:
        render = conn.execute("SELECT * FROM renders WHERE clip_id = %s ORDER BY id LIMIT 1", (clip["id"],)).fetchone()
        work = settings.dir("work", f"clip{clip['id']}")
        frames = [media.extract_frame(render["file_path"], render["duration_s"] * (k + 0.5) / 4,
                                      work / f"check_{k}.jpg", width=540) for k in range(4)]
        result = llm.final_check(settings.channel(clip["channel_key"]), dict(clip), frames, game=clip["game"],
                                 block_tags=sorted(block))
        flagged = sorted(block.intersection(result["policy_tags"]))
        if not result["pass"] or flagged:
            status, reason = "failed_check", "; ".join(result["issues"] + [f"blocked tag: {t}" for t in flagged])
        elif settings.require_approval:
            status, reason = "pending_approval", None
        else:
            status, reason = "approved", None
        conn.execute(
            """UPDATE clips SET status = %s, status_reason = %s, check_result = %s, policy_tags = %s,
               approved_by = %s, approved_at = CASE WHEN %s = 'approved' THEN now() ELSE NULL END WHERE id = %s""",
            (status, reason, db.Jsonb(result), result["policy_tags"],
             "auto (approval step disabled)" if status == "approved" else None, status, clip["id"]))
        db.log_event(conn, "final_check", "clip", clip["id"], status, result)
        out[status].append(clip["id"])
    conn.commit()
    return out


def approval_queue(conn) -> list[dict]:
    return conn.execute(
        """SELECT cl.id, ch.key AS channel, cl.for_date, cl.script->>'hook' AS hook, cl.titles,
                  cl.check_result, (SELECT array_agg(file_path) FROM renders r WHERE r.clip_id = cl.id) AS files
           FROM clips cl JOIN channels ch ON ch.id = cl.channel_id
           WHERE cl.status = 'pending_approval' ORDER BY cl.for_date, cl.id""").fetchall()


def decide(conn, clip_id: int, *, approve: bool, by: str, reason: str | None = None,
           tiktok_options: dict | None = None) -> dict:
    """Record the reviewer's decision. ``tiktok_options`` are the reviewer's own per-post TikTok choices."""
    clip = conn.execute("SELECT * FROM clips WHERE id = %s", (clip_id,)).fetchone()
    if clip is None or clip["status"] != "pending_approval":
        raise ValueError(f"clip {clip_id} is not waiting for approval")
    if not by:
        raise ValueError("the reviewer's name is required")
    status = "approved" if approve else "rejected"
    check = dict(clip["check_result"] or {})
    if tiktok_options is not None:
        check["tiktok_options"] = tiktok_options
    conn.execute(
        "UPDATE clips SET status = %s, status_reason = %s, approved_by = %s, approved_at = now(), check_result = %s WHERE id = %s",
        (status, reason, by, db.Jsonb(check), clip_id))
    db.log_event(conn, "approval", "clip", clip_id, status, {"by": by, "reason": reason})
    conn.commit()
    return {"clip_id": clip_id, "status": status}
