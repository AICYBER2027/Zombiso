"""Original commentary, captions and per-platform titles for each selected clip."""

from __future__ import annotations

import re

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


PAUSE_BREAK_S = 0.6
MAX_CUE_S = 2.5
_END_PUNCT = re.compile(r"[.!?,;:…]$")


def transcript_cues(segments, duration: float, *, words_per_cue: int = WORDS_PER_CUE) -> list[dict]:
    """Subtitles from what is actually said: Whisper word timings (relative to the clip start).

    Words are grouped into short cues, breaking at punctuation, at pauses and every few words, and each
    cue is shown exactly while its words are spoken (lightly extended so short words stay readable).
    """
    words = [w for seg in segments for w in (seg.words if hasattr(seg, "words") else seg.get("words", []))]
    words = [w for w in words if w["end"] > 0 and w["start"] < duration]
    cues: list[dict] = []
    group: list[dict] = []

    def flush():
        if group:
            cues.append({"start": max(0.0, group[0]["start"]), "end": min(duration, group[-1]["end"]),
                         "text": " ".join(w["text"] for w in group), "kind": "speech"})
            group.clear()

    for w in words:
        if group and (w["start"] - group[-1]["end"] > PAUSE_BREAK_S or w["end"] - group[0]["start"] > MAX_CUE_S):
            flush()
        group.append(w)
        if len(group) >= words_per_cue or _END_PUNCT.search(w["text"]):
            flush()
    flush()
    for i, c in enumerate(cues):                     # minimum on-screen time, never overlapping the next cue
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else duration
        c["end"] = round(min(max(c["end"], c["start"] + 0.7), nxt), 3)
        c["start"] = round(c["start"], 3)
    return [c for c in cues if c["end"] > c["start"]]


def use_transcript(settings: Settings) -> bool:
    if settings.captions == "transcript":
        return True
    return settings.captions == "auto" and settings.transcriber == "whisper"


def build_captions(settings: Settings, clip: dict, result: dict, transcriber=None) -> tuple[list[dict], str]:
    """Caption cues for a clip and where they came from: 'transcript' (real speech) or 'script'."""
    duration = clip["out_s"] - clip["in_s"]
    if use_transcript(settings) and clip.get("file_path"):
        from . import signals

        tr = transcriber or signals.make_transcriber(settings)
        segments = tr.transcribe(clip["file_path"], start=clip["in_s"], duration=duration)
        speech = transcript_cues(segments, duration)
        if speech or settings.captions == "transcript":
            if settings.llm_mode == "mock":
                # The offline mock writes generic hooks that may not match this clip; show only what is said.
                return speech, "transcript" if speech else "none (no speech heard)"
            hook_end = min(HOOK_WINDOW_S, duration * 0.25)
            # The hook stays as a short title card at the top; the spoken words are the subtitles.
            hook = [{"start": 0.0, "end": round(hook_end, 3), "text": result["hook"], "kind": "hook_top"}]
            return hook + speech, "transcript" if speech else "none (no speech heard)"
    return caption_cues(result, duration), "script"


def write_scripts(conn, settings: Settings, llm: LLM, *, clip_ids: list[int] | None = None, transcriber=None) -> dict:
    rows = conn.execute(
        """SELECT cl.*, ch.key AS channel_key, s.game, s.file_path, r.credit_text FROM clips cl
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
        captions, caption_source = build_captions(settings, dict(clip), result, transcriber)
        script = {"hook": result["hook"], "body": result["body"], "closer": result["closer"],
                  "angle": result["angle"], "language": clip["language"],
                  "captions": captions, "caption_source": caption_source}
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
