"""Original commentary, captions and per-platform titles for each selected clip."""

from __future__ import annotations

from . import db, dedupe
from .config import Settings
from .llm import LLM

HOOK_WINDOW_S = 2.0
CLOSER_WINDOW_S = 2.5
WORDS_PER_CUE = 4


def caption_cues(script: dict, duration: float) -> list[dict]:
    """Split hook/body/closer into short burned-in caption cues spread over the clip.

    Timing is estimated (hook in the first 2 s, closer at the end). When the owner's voice
    recording exists, real word timings could replace this; see README.
    """
    duration = max(duration, 3.0)
    parts = [("hook", script["hook"], 0.0, min(HOOK_WINDOW_S, duration * 0.25)),
             ("body", script["body"], min(HOOK_WINDOW_S, duration * 0.25), max(duration - CLOSER_WINDOW_S, duration * 0.6)),
             ("closer", script["closer"], max(duration - CLOSER_WINDOW_S, duration * 0.6), duration)]
    cues = []
    for kind, text, start, end in parts:
        words = text.split()
        if not words or end <= start:
            continue
        chunks = [" ".join(words[i:i + WORDS_PER_CUE]) for i in range(0, len(words), WORDS_PER_CUE)]
        step = (end - start) / len(chunks)
        for i, chunk in enumerate(chunks):
            cues.append({"start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3),
                         "text": chunk, "kind": kind})
    return cues


def write_scripts(conn, settings: Settings, llm: LLM, *, clip_ids: list[int] | None = None) -> dict:
    rows = conn.execute(
        """SELECT cl.*, ch.key AS channel_key, s.game, r.credit_text FROM clips cl
           JOIN channels ch ON ch.id = cl.channel_id
           JOIN candidates c ON c.id = cl.candidate_id JOIN sources s ON s.id = c.source_id
           LEFT JOIN rights_ledger r ON r.id = s.rights_id
           WHERE cl.status = 'selected' AND (%s::int[] IS NULL OR cl.id = ANY(%s::int[])) ORDER BY cl.id""",
        (clip_ids, clip_ids)).fetchall()
    out = {"scripted": [], "duplicate": []}
    for clip in rows:
        channel = settings.channel(clip["channel_key"])
        frames = sorted(settings.dir("frames").glob(f"cand{clip['candidate_id']}_*.jpg"))
        result = llm.script(channel, dict(clip), frames, game=clip["game"])
        script = {"hook": result["hook"], "body": result["body"], "closer": result["closer"],
                  "angle": result["angle"], "language": clip["language"],
                  "captions": caption_cues(result, clip["out_s"] - clip["in_s"])}
        titles = {p: dict(result[p]) for p in ("youtube", "instagram", "tiktok")}
        if clip["credit_text"]:
            # Licences such as CC BY or a creator's terms require attribution: put the credit in every description.
            for copy in titles.values():
                if clip["credit_text"] not in copy["description"]:
                    copy["description"] = f"{copy['description']}\n\n{clip['credit_text']}".strip()
        text = " ".join([result["hook"], result["body"], result["closer"], titles["youtube"]["title"]])
        dup = dedupe.check_script(conn, channel_id=clip["channel_id"], clip_id=clip["id"], script_text=text,
                                  angle=result["angle"])
        status = "duplicate" if dup.duplicate else "scripted"
        conn.execute(
            "UPDATE clips SET script = %s, titles = %s, script_text = %s, status = %s, status_reason = %s WHERE id = %s",
            (db.Jsonb(script), db.Jsonb(titles), text, status, "; ".join(dup.reasons) or None, clip["id"]))
        db.log_event(conn, "script", "clip", clip["id"], status, {"reasons": dup.reasons})
        out[status].append(clip["id"])
    conn.commit()
    return out
